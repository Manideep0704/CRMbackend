import asyncio
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from .db import get_crm_db, get_db, init_mongo_indexes, now, uid
from .imports import MAX_BYTES, commit_job, parse_file, preview_job, safe_csv_cell
from .models import (
    Activity,
    AuditLog,
    AuthSession,
    FollowUp,
    ImportErrorRow,
    ImportJob,
    Lead,
    Membership,
    ModelDict,
    Organization,
    User,
    Website,
    WebsiteKey,
)
from .schemas import (
    CommitInput,
    FollowUpInput,
    FollowUpUpdate,
    LeadInput,
    Login,
    MembershipInput,
    MemberUpdate,
    MergeInput,
    NoteInput,
    WebsiteInput,
)
from .security import (
    DUMMY_PASSWORD,
    admin,
    context,
    digest,
    hash_password,
    is_trusted_origin,
    limiter,
    new_session,
    revoke_user_sessions,
    verify_password,
    writer,
)
from .services import activity, audit, check_assignee, create_lead, scoped, serialize, sync_next_follow_up

app = FastAPI(title='Timely CRM', version='1.0.0')

# CORS: exact origin allowlist only; no wildcard regex. Configure FRONTEND_ORIGIN in env.
_CORS_ORIGINS = {
    'http://localhost:3000',
    'http://127.0.0.1:3000',
    'https://crmfrontend.iryax.com',
    'http://crmfrontend.iryax.com',
    'http://62.72.29.27:3081',
    'https://62.72.29.27:3081',
    'https://crmbackend.iryax.com',
    'http://crmbackend.iryax.com',
    'http://62.72.29.27:8010',
    'https://62.72.29.27:8010',
}
for o in os.getenv('FRONTEND_ORIGIN', '').split(','):
    clean = o.strip().rstrip('/')
    if clean and clean != '*':
        _CORS_ORIGINS.add(clean)

if os.getenv('APP_ENV') != 'production':
    _CORS_ORIGINS.update([
        'http://192.168.1.103:3000',
    ])

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_CORS_ORIGINS),
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'],
    allow_headers=['Content-Type', 'X-Timely-Request', 'X-Api-Key', 'Idempotency-Key', 'Authorization'],
)


def ensure_db_schema():
    """Initializes MongoDB indexes and ensures required seed data exists."""
    init_mongo_indexes()
    db = get_crm_db()

    # 1. Organization
    org = db['organizations'].find_one({'slug': 'timely-health'})
    if not org:
        org = Organization(name="Timely Health", slug="timely-health", status="active")
        db['organizations'].insert_one(org)
    else:
        org = ModelDict(org)

    # 2. Website
    website = db['websites'].find_one({'organization_id': org.id, 'domain': 'digital.timelyhealth.in'})
    if not website:
        website = Website(
            organization_id=org.id,
            name="Timely Health Digital",
            domain="digital.timelyhealth.in",
            status="active",
            config={}
        )
        db['websites'].insert_one(website)
    else:
        website = ModelDict(website)

    # 3. Controlled initial admin provisioning via env or defaults
    init_admin_email = os.getenv("INITIAL_ADMIN_EMAIL") or os.getenv("ADMIN_EMAIL", "admin@timelyhealth.in")
    init_admin_pass = os.getenv("INITIAL_ADMIN_PASSWORD") or os.getenv("ADMIN_PASS", "TimelyAdmin@2026")
    if init_admin_email and init_admin_pass:
        email_clean = init_admin_email.lower().strip()
        user = db['users'].find_one({'email': email_clean})
        if not user:
            user = User(
                name="Administrator",
                email=email_clean,
                password_hash=hash_password(init_admin_pass),
                is_active=True,
                auth_provider='local',
                token_version=1
            )
            db['users'].insert_one(user)
        user_id = user.get('id') or str(user.get('_id'))
        mem_id = f"{org.id}:{user_id}"
        if not db['memberships'].find_one({'$or': [{'_id': mem_id}, {'organization_id': org.id, 'user_id': user_id}]}):
            db['memberships'].insert_one(Membership(organization_id=org.id, user_id=user_id, role="admin", status="active"))

    # 4. Controlled initial website API key
    init_key = os.getenv("INITIAL_WEBSITE_KEY") or os.getenv("WEBSITE_KEY", "tly_X6vBwOKDgBXupQlwFYnkWcOci5gtIlvQT52w_bboJRc")
    if init_key and website:
        key_hash = digest(init_key)
        if not db['website_keys'].find_one({'website_id': website.id, 'key_hash': key_hash}):
            db['website_keys'].insert_one(WebsiteKey(organization_id=org.id, website_id=website.id, key_hash=key_hash))


try:
    ensure_db_schema()
except Exception as _schema_err:
    print(f"[Pre-startup Schema Notice]: {_schema_err}")


@app.on_event("startup")
def startup_db_init():
    ensure_db_schema()
    try:
        from .mongo import sync_sales_agents_from_mongo
        sync_sales_agents_from_mongo()
    except Exception as e:
        print(f"[Sales Sync Startup Error]: {e}")
    asyncio.create_task(sync_website_leads_fallback_worker())


# ==============================================================================
# FALLBACK RECONCILIATION WORKER
# ==============================================================================
async def sync_website_leads_fallback_worker():
    await asyncio.sleep(5)
    while True:
        try:
            from .mongo import sync_leads_from_mongo
            synced = sync_leads_from_mongo()
            if synced > 0:
                print(f"[Fallback Ingest Worker] Successfully imported {synced} lead(s) from company MongoDB.")
        except Exception as e:
            print(f"[Fallback Ingest Worker Error]: {e}")
        await asyncio.sleep(30)


@app.get('/api/v1/public/ingest-status')
def ingest_status(db=Depends(get_db)):
    """Public read-only health check for lead ingestion infrastructure."""
    now_utc = datetime.now(timezone.utc)
    ten_min_ago = now_utc - timedelta(minutes=10)
    recent_count = db['leads'].count_documents({'created_at': {'$gte': ten_min_ago}})
    last_lead = db['leads'].find_one(sort=[('created_at', -1)])
    last_received = last_lead.get('created_at') if last_lead else None
    return {
        'status': 'healthy',
        'pipeline': 'dual-channel',
        'channels': {
            'primary_webhook': 'active',
            'fallback_mongodb_reconciliation': 'active (every 30s)'
        },
        'leads_last_10m': recent_count,
        'last_lead_at': last_received.isoformat() if last_received else None,
        'timestamp': now_utc.isoformat()
    }


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return JSONResponse(status_code=422, content={'detail': [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in error.errors()]})


@app.exception_handler(DuplicateKeyError)
async def duplicate_key_error(request, error):
    return JSONResponse(status_code=409, content={'detail': 'Record conflicts with an existing record. Refresh and retry.'})


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, error: Exception):
    import traceback
    trace = traceback.format_exc()
    print(f"[UNHANDLED EXCEPTION] {request.method} {request.url.path}: {error}\n{trace}")
    return JSONResponse(status_code=500, content={'detail': f"{type(error).__name__}: {str(error)}"})


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@app.get('/')
def root():
    return {
        'service': 'Timely CRM Backend API',
        'status': 'online',
        'database': 'MongoDB Atlas',
        'health': '/health',
        'docs': '/docs',
        'routes': {
            'leads': '/api/v1/leads',
            'dashboard': '/api/v1/dashboard/summary',
            'login': '/api/v1/auth/login',
            'webhook': '/api/v1/public/leads'
        }
    }


@app.get('/health')
def health(db=Depends(get_db)):
    crm_connected = False
    try:
        db.command('ping')
        crm_connected = True
    except Exception:
        crm_connected = False

    from .mongo import AUTH_MONGO_URI, CRM_MONGO_URI, MONGO_URI, get_attendance_db
    auth_configured = bool(AUTH_MONGO_URI)
    auth_connected = False
    if auth_configured:
        try:
            att_db = get_attendance_db()
            if att_db is not None:
                att_db.command('ping')
                auth_connected = True
        except Exception:
            auth_connected = False
    return {
        'status': 'ok' if crm_connected else 'degraded',
        'service': 'Timely CRM',
        'database': 'MongoDB Atlas (crmDB)',
        'data_storage': {
            'configured': bool(CRM_MONGO_URI),
            'connected': crm_connected
        },
        'attendance_directory': {
            'configured': auth_configured,
            'connected': auth_connected
        }
    }


@app.post('/api/v1/auth/login')
def login(payload: Login, request: Request, response: Response, db=Depends(get_db)):
    login_limit = 5 if os.getenv('APP_ENV') == 'production' else 100
    limiter.check(('login', request.client.host), login_limit, 300)
    origin = request.headers.get('origin')
    referer = request.headers.get('referer', '')
    is_swagger = '/docs' in referer or '/redoc' in referer
    if not is_swagger:
        if request.headers.get('X-Timely-Request') != '1' or not is_trusted_origin(origin):
            raise HTTPException(403, 'Untrusted sign-in request')
    else:
        if origin and not is_trusted_origin(origin):
            raise HTTPException(403, 'Untrusted sign-in request')

    org_doc = db['organizations'].find_one({'slug': payload.organization_slug, 'status': 'active'})
    if not org_doc:
        raise HTTPException(401, 'Invalid email, password or organization')
    org = ModelDict(org_doc)

    user_doc = db['users'].find_one({'email': payload.email.lower().strip()})
    user = ModelDict(user_doc) if user_doc else None

    from .mongo import authenticate_attendance_user, check_attendance_user_status
    is_federated = bool(
        (user and getattr(user, 'auth_provider', None) == 'attendance')
        or (user and getattr(user, 'auth_provider', None) is None and user.email.endswith('@timelyhealth.in') and user.email != 'admin@timelyhealth.in')
    )
    exists_in_dir, provider_ok = check_attendance_user_status(payload.email)
    if not is_federated and exists_in_dir:
        is_federated = True

    mongo_user = None
    if is_federated:
        if not provider_ok:
            raise HTTPException(503, 'External authentication provider is temporarily unavailable. Please try again later.')
        mongo_user = authenticate_attendance_user(payload.email, payload.password)
        valid = mongo_user is not None
    else:
        valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD)

    if mongo_user:
        incoming_sub = str(mongo_user.get('_id', '')).strip()
        incoming_email = mongo_user['email'].lower().strip()

        user_by_sub = db['users'].find_one({'auth_provider': 'attendance', 'auth_subject': incoming_sub}) if incoming_sub else None
        user_by_email = db['users'].find_one({'email': incoming_email})

        if user_by_sub and user_by_email and (user_by_sub.get('id') or str(user_by_sub.get('_id'))) != (user_by_email.get('id') or str(user_by_email.get('_id'))):
            raise HTTPException(403, 'Identity collision: external subject and email belong to different accounts.')

        if user_by_sub:
            user = ModelDict(user_by_sub)
            if user.email != incoming_email:
                user.email = incoming_email
                db['users'].update_one({'_id': user_by_sub['_id']}, {'$set': {'email': incoming_email}})
        elif user_by_email:
            if user_by_email.get('auth_provider') == 'local':
                raise HTTPException(403, 'A local account already exists with this email. Federated linking requires administrator action.')
            if user_by_email.get('auth_subject') and user_by_email.get('auth_subject') != incoming_sub:
                raise HTTPException(403, 'External identity subject mismatch for this account.')
            user = ModelDict(user_by_email)
            user.auth_provider = 'attendance'
            user.auth_subject = incoming_sub
            db['users'].update_one({'_id': user_by_email['_id']}, {'$set': {'auth_provider': 'attendance', 'auth_subject': incoming_sub}})
        else:
            new_user = User(
                name=mongo_user.get('name') or 'User',
                email=incoming_email,
                password_hash=hash_password(secrets.token_urlsafe(32)),
                is_active=True,
                auth_provider='attendance',
                auth_subject=incoming_sub,
                token_version=1
            )
            db['users'].insert_one(new_user)
            user = new_user

        if not user.is_active:
            raise HTTPException(403, 'Account is inactive or suspended in CRM.')

        rand_hash = hash_password(secrets.token_urlsafe(32))
        update_fields = {'password_hash': rand_hash}
        if mongo_user.get('name') and not user.name:
            user.name = mongo_user['name']
            update_fields['name'] = mongo_user['name']
        db['users'].update_one({'$or': [{'_id': user.id}, {'id': user.id}]}, {'$set': update_fields})

        entitled_slug = mongo_user.get('entitled_org_slug', 'timely-health')
        mem_id = f"{org.id}:{user.id}"
        membership_check = db['memberships'].find_one({'$or': [{'_id': mem_id}, {'organization_id': org.id, 'user_id': user.id}]})

        if not membership_check:
            if org.slug != entitled_slug:
                raise HTTPException(401, 'Invalid email, password or organization')
            allowed_roles = {'admin', 'agent', 'viewer'}
            raw_role = mongo_user.get('role', 'viewer')
            safe_role = raw_role if raw_role in allowed_roles else 'viewer'
            new_mem = Membership(
                organization_id=org.id,
                user_id=user.id,
                role=safe_role,
                status='active'
            )
            db['memberships'].insert_one(new_mem)
            membership_check = new_mem
        else:
            membership_check = ModelDict(membership_check)
            if membership_check.status != 'active':
                raise HTTPException(403, 'Organization access is inactive')
            if org.slug == entitled_slug and mongo_user.get('role'):
                allowed_roles = {'admin', 'agent', 'viewer'}
                raw_role = mongo_user.get('role', membership_check.role)
                current_role = membership_check.role
                if current_role == 'admin':
                    raw_role = 'admin'
                elif current_role == 'agent' and raw_role == 'viewer':
                    raw_role = 'agent'
                if raw_role in allowed_roles and raw_role != membership_check.role:
                    membership_check.role = raw_role
                    db['memberships'].update_one({'_id': membership_check['_id']}, {'$set': {'role': raw_role}})
        valid = True

    mem_id = f"{org.id}:{user.id}" if user else ""
    membership = ModelDict(db['memberships'].find_one({'$or': [{'_id': mem_id}, {'organization_id': org.id, 'user_id': user.id}]})) if user else None

    if not valid or not user or not user.is_active or not membership or membership.status != 'active':
        raise HTTPException(401, 'Invalid email, password or organization')

    token = new_session(db, user, org.id)
    is_production = os.getenv('APP_ENV') == 'production'
    response.set_cookie('timely_session', token, httponly=True, secure=is_production, samesite='lax', max_age=43200, path='/')
    return {'user': serialize(user), 'organization': serialize(org), 'role': membership.role}


@app.post('/api/v1/auth/logout')
def logout(request: Request, response: Response, ctx=Depends(context), db=Depends(get_db)):
    token = request.cookies.get('timely_session', '')
    if token:
        token_hash = digest(token)
        db['auth_sessions'].delete_one({'$or': [{'_id': token_hash}, {'token_hash': token_hash}]})
    response.delete_cookie('timely_session', path='/')
    return {'ok': True}


@app.post('/api/v1/auth/revoke-all')
def revoke_all(response: Response, ctx=Depends(context), db=Depends(get_db)):
    revoke_user_sessions(db, ctx['user_id'])
    response.delete_cookie('timely_session', path='/')
    return {'ok': True, 'message': 'All active sessions revoked'}


@app.get('/api/v1/organizations/me')
def me(ctx=Depends(context)):
    return {'user': serialize(ctx['user']), 'organization': serialize(ctx['organization']), 'role': ctx['role']}


@app.get('/api/v1/members')
def members(ctx=Depends(context), db=Depends(get_db)):
    mem_docs = list(db['memberships'].find({'organization_id': ctx['organization_id']}))
    result = []
    for m in mem_docs:
        u = db['users'].find_one({'$or': [{'_id': m['user_id']}, {'id': m['user_id']}]})
        if u:
            u_clean = serialize(u)
            result.append({**u_clean, 'role': m.get('role', 'viewer'), 'membership_status': m.get('status', 'active')})
    return result


@app.post('/api/v1/members', status_code=201)
def add_member(payload: MembershipInput, ctx=Depends(admin), db=Depends(get_db)):
    email_clean = payload.email.lower().strip()
    if db['users'].find_one({'email': email_clean}):
        raise HTTPException(409, 'Email is already registered. Existing-user invitations are outside this MVP.')
    user = User(name=payload.name, email=email_clean, password_hash=hash_password(payload.password))
    db['users'].insert_one(user)
    db['memberships'].insert_one(Membership(organization_id=ctx['organization_id'], user_id=user.id, role=payload.role))
    audit(db, ctx, 'member.created', user.id)
    return serialize(user)


@app.patch('/api/v1/members/{user_id}')
def update_member_role(user_id: str, payload: MemberUpdate, ctx=Depends(context), db=Depends(get_db)):
    is_admin = ctx['role'] == 'admin'
    is_self = ctx['user_id'] == user_id
    if not is_admin and not is_self:
        raise HTTPException(403, 'Administrator access required')

    mem_id = f"{ctx['organization_id']}:{user_id}"
    membership = db['memberships'].find_one({
        '$or': [{'_id': mem_id}, {'organization_id': ctx['organization_id'], 'user_id': user_id}]
    })
    if not membership:
        raise HTTPException(404, 'Member not found in this organization')
    user = db['users'].find_one({'$or': [{'_id': user_id}, {'id': user_id}]})
    if not user:
        raise HTTPException(404, 'User not found')

    user_updates = {}
    if payload.name is not None and is_admin:
        user_updates['name'] = payload.name
    if payload.email is not None and is_admin:
        existing = db['users'].find_one({'email': payload.email, '_id': {'$ne': user['_id']}, 'id': {'$ne': user['id']}})
        if existing:
            raise HTTPException(409, 'Email is already in use by another account')
        user_updates['email'] = payload.email
    if user_updates:
        db['users'].update_one({'_id': user['_id']}, {'$set': user_updates})

    if payload.role is not None:
        if not is_admin:
            raise HTTPException(403, 'Only administrators can update member roles')
        db['memberships'].update_one({'_id': membership['_id']}, {'$set': {'role': payload.role}})

    updated_user = db['users'].find_one({'_id': user['_id']})
    updated_mem = db['memberships'].find_one({'_id': membership['_id']})
    return {
        **serialize(updated_user),
        'role': updated_mem.get('role', 'agent'),
        'membership_status': updated_mem.get('status', 'active')
    }


@app.get('/api/v1/websites')
def websites(ctx=Depends(context), db=Depends(get_db)):
    sites = list(db['websites'].find({'organization_id': ctx['organization_id']}).sort('created_at', 1))
    return [serialize(w) for w in sites]


@app.post('/api/v1/websites', status_code=201)
def add_website(payload: WebsiteInput, ctx=Depends(admin), db=Depends(get_db)):
    check_assignee(db, ctx['organization_id'], payload.assigned_to)
    website = Website(
        organization_id=ctx['organization_id'],
        name=payload.name,
        domain=payload.domain,
        status=payload.status,
        config={'field_mapping': payload.field_mapping, 'assigned_to': payload.assigned_to}
    )
    db['websites'].insert_one(website)
    audit(db, ctx, 'website.created', website.id)
    return serialize(website)


@app.patch('/api/v1/websites/{website_id}')
def edit_website(website_id: str, payload: WebsiteInput, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, 'websites', website_id, ctx['organization_id'])
    check_assignee(db, ctx['organization_id'], payload.assigned_to)
    db['websites'].update_one(
        {'$or': [{'_id': website_id}, {'id': website_id}]},
        {'$set': {
            'name': payload.name,
            'domain': payload.domain,
            'status': payload.status,
            'config': {'field_mapping': payload.field_mapping, 'assigned_to': payload.assigned_to}
        }}
    )
    audit(db, ctx, 'website.updated', website.id)
    updated = db['websites'].find_one({'$or': [{'_id': website_id}, {'id': website_id}]})
    return serialize(updated)


@app.post('/api/v1/websites/{website_id}/rotate-key')
def rotate_key(website_id: str, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, 'websites', website_id, ctx['organization_id'])
    db['website_keys'].update_many(
        {'website_id': website.id, 'revoked_at': None},
        {'$set': {'revoked_at': now()}}
    )
    token = 'tly_' + secrets.token_urlsafe(40)
    db['website_keys'].insert_one(WebsiteKey(
        organization_id=ctx['organization_id'],
        website_id=website.id,
        key_hash=digest(token)
    ))
    audit(db, ctx, 'website.key_rotated', website.id)
    return {'key': token, 'message': 'Shown once. Store only in your website server environment. Old keys are revoked.'}


@app.delete('/api/v1/websites/{website_id}/key')
def revoke_key(website_id: str, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, 'websites', website_id, ctx['organization_id'])
    db['website_keys'].update_many(
        {'website_id': website.id, 'revoked_at': None},
        {'$set': {'revoked_at': now()}}
    )
    audit(db, ctx, 'website.key_revoked', website.id)
    return {'ok': True}


def agent_scope(ctx) -> str | None:
    return ctx['user_id'] if ctx['role'] == 'agent' else None


def build_lead_filter(ctx, q='', status='', website_id='', source='', assigned_to='', date_from=None, date_to=None):
    query = {
        'organization_id': ctx['organization_id'],
        'deleted_at': None
    }
    if q:
        pattern = re.escape(q)
        query['$or'] = [
            {'full_name': {'$regex': pattern, '$options': 'i'}},
            {'email': {'$regex': pattern, '$options': 'i'}},
            {'phone': {'$regex': pattern, '$options': 'i'}},
            {'company_name': {'$regex': pattern, '$options': 'i'}}
        ]
    if status:
        query['status'] = status
    if website_id:
        query['website_id'] = website_id
    if source:
        query['source'] = source
    if assigned_to:
        if assigned_to == 'unassigned':
            query['$or'] = [
                {'assigned_to': None},
                {'assigned_to': ''},
                {'assigned_to': {'$exists': False}}
            ]
        else:
            query['assigned_to'] = assigned_to

    date_filter = {}
    if date_from:
        if date_from.tzinfo is None:
            raise HTTPException(422, 'Date filters must include a timezone')
        date_filter['$gte'] = date_from.astimezone(timezone.utc)
    if date_to:
        if date_to.tzinfo is None:
            raise HTTPException(422, 'Date filters must include a timezone')
        date_filter['$lte'] = date_to.astimezone(timezone.utc)
    if date_filter:
        query['created_at'] = date_filter

    return query


@app.get('/api/v1/leads')
def leads(
    q: str = '',
    status: str = '',
    website_id: str = '',
    source: str = '',
    assigned_to: str = '',
    unclaimed: bool = False,
    pipeline: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = 'newest',
    ctx=Depends(context),
    db=Depends(get_db)
):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, 'Invalid pagination')

    if unclaimed:
        query = {
            'organization_id': ctx['organization_id'],
            'deleted_at': None,
            'accepted_by': None
        }
        if q:
            pattern = re.escape(q)
            query['$or'] = [
                {'full_name': {'$regex': pattern, '$options': 'i'}},
                {'email': {'$regex': pattern, '$options': 'i'}},
                {'phone': {'$regex': pattern, '$options': 'i'}},
                {'company_name': {'$regex': pattern, '$options': 'i'}}
            ]
        if status:
            query['status'] = status
        if website_id:
            query['website_id'] = website_id
        if source:
            query['source'] = source
        if date_from or date_to:
            df = {}
            if date_from: df['$gte'] = date_from.astimezone(timezone.utc)
            if date_to: df['$lte'] = date_to.astimezone(timezone.utc)
            query['created_at'] = df
    else:
        eff_assigned_to = agent_scope(ctx) or assigned_to
        query = build_lead_filter(ctx, q, status, website_id, source, eff_assigned_to, date_from, date_to)

    sort_order = [('created_at', -1), ('_id', 1)]
    if sort == 'oldest':
        sort_order = [('created_at', 1), ('_id', 1)]
    elif sort == 'name':
        sort_order = [('full_name', 1), ('_id', 1)]
    elif sort != 'newest':
        raise HTTPException(422, 'Sort must be newest, oldest or name')

    total = db['leads'].count_documents(query)
    cursor = db['leads'].find(query).sort(sort_order).skip((page - 1) * page_size).limit(page_size)
    items = [serialize(lead) for lead in cursor]

    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


@app.get('/api/v1/leads/queue')
def leads_queue(q: str = '', status: str = '', website_id: str = '', source: str = '', page: int = 1, page_size: int = 20, sort: str = 'newest', ctx=Depends(context), db=Depends(get_db)):
    return leads(
        q=q,
        status=status,
        website_id=website_id,
        source=source,
        unclaimed=True,
        page=page,
        page_size=page_size,
        sort=sort,
        ctx=ctx,
        db=db
    )


@app.post('/api/v1/leads', status_code=201)
def add_lead(payload: LeadInput, ctx=Depends(writer), db=Depends(get_db)):
    lead, _ = create_lead(db, ctx['organization_id'], payload, 'manual', ctx['user_id'])
    return serialize(lead)


@app.get('/api/v1/leads/export')
def export_leads(
    q: str = '',
    status: str = '',
    website_id: str = '',
    source: str = '',
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    ctx=Depends(admin),
    db=Depends(get_db)
):
    from zoneinfo import ZoneInfo
    ist = ZoneInfo('Asia/Kolkata')
    query = build_lead_filter(ctx, q=q, status=status, website_id=website_id, source=source, date_from=date_from, date_to=date_to)
    lead_cursor = db['leads'].find(query).sort('created_at', -1)
    leads_list = list(lead_cursor)

    output = io.StringIO()
    csv_writer = csv.writer(output)
    csv_writer.writerow([
        'S.No', 'Lead ID', 'Full Name', 'Phone', 'Email',
        'Company / Clinic', 'Inquiry / Looking For', 'Status',
        'Priority', 'Source', 'Website', 'Assigned To',
        'Next Follow-Up', 'Created Date'
    ])

    websites_dict = {w['id']: w['name'] for w in db['websites'].find({'organization_id': ctx['organization_id']})}
    mem_docs = list(db['memberships'].find({'organization_id': ctx['organization_id']}))
    members_dict = {}
    for m in mem_docs:
        u = db['users'].find_one({'$or': [{'_id': m['user_id']}, {'id': m['user_id']}]})
        if u:
            members_dict[m['user_id']] = u.get('name', 'Agent')

    for idx, lead in enumerate(leads_list, 1):
        lead_id = lead.get('id') or str(lead.get('_id'))
        raw_p = (lead.get('phone') or '').replace("'", "").strip()
        p_digits = re.sub(r'\D', '', raw_p)
        if len(p_digits) == 12 and p_digits.startswith('91'):
            clean_phone = p_digits[2:]
        elif len(p_digits) == 11 and p_digits.startswith('0'):
            clean_phone = p_digits[1:]
        elif len(p_digits) == 10:
            clean_phone = p_digits
        else:
            clean_phone = p_digits or raw_p

        src = lead.get('source', '')
        formatted_source = 'Website Form' if src == 'website' else 'Manual Entry' if src == 'manual' else src.upper()

        next_follow = '-'
        fu_dt = lead.get('next_follow_up_at')
        if fu_dt:
            dt = fu_dt if fu_dt.tzinfo else fu_dt.replace(tzinfo=timezone.utc)
            next_follow = dt.astimezone(ist).strftime('%d-%b-%Y %I:%M %p')

        created_str = '-'
        cr_dt = lead.get('created_at')
        if cr_dt:
            dt = cr_dt if cr_dt.tzinfo else cr_dt.replace(tzinfo=timezone.utc)
            created_str = dt.astimezone(ist).strftime('%d-%b-%Y %I:%M %p')

        csv_writer.writerow([
            idx,
            safe_csv_cell(f"TL-{lead_id[:8].upper()}"),
            safe_csv_cell(lead.get('full_name') or ''),
            safe_csv_cell(clean_phone),
            safe_csv_cell(lead.get('email') or ''),
            safe_csv_cell(lead.get('company_name') or ''),
            safe_csv_cell(lead.get('inquiry_type') or ''),
            safe_csv_cell((lead.get('status') or '').capitalize()),
            safe_csv_cell((lead.get('priority') or '').capitalize()),
            safe_csv_cell(formatted_source),
            safe_csv_cell(websites_dict.get(lead.get('website_id'), 'Timely Health Digital')),
            safe_csv_cell(members_dict.get(lead.get('assigned_to'), 'Unassigned')),
            safe_csv_cell(next_follow),
            safe_csv_cell(created_str),
        ])

    audit(db, ctx, 'leads.exported', f'count:{len(leads_list)}')
    filename = f"timely-leads-export-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        output.getvalue(),
        media_type='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )


@app.get('/api/v1/leads/{lead_id}')
def get_lead(lead_id: str, ctx=Depends(context), db=Depends(get_db)):
    lead = scoped(db, 'leads', lead_id, ctx['organization_id'])
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id'] and lead.accepted_by is not None:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    events = list(db['activities'].find({'organization_id': ctx['organization_id'], 'lead_id': lead.id}).sort('created_at', -1))
    return {**serialize(lead), 'activities': [serialize(e) for e in events]}


@app.post('/api/v1/leads/{lead_id}/accept')
def accept_lead(lead_id: str, ctx=Depends(context), db=Depends(get_db)):
    # Auto-elevate viewer to agent if attempting to accept a lead
    if ctx['role'] == 'viewer':
        mem_id = f"{ctx['organization_id']}:{ctx['user_id']}"
        db['memberships'].update_one(
            {'$or': [{'_id': mem_id}, {'organization_id': ctx['organization_id'], 'user_id': ctx['user_id']}]},
            {'$set': {'role': 'agent', 'status': 'active'}},
            upsert=True
        )
        ctx['role'] = 'agent'

    lead = scoped(db, 'leads', lead_id, ctx['organization_id'])

    # Idempotent: already accepted by this user
    if lead.accepted_by == ctx['user_id']:
        return serialize(lead)

    # If accepted by someone else and not assigned to this user
    if lead.accepted_by and lead.accepted_by != ctx['user_id'] and lead.assigned_to != ctx['user_id'] and ctx['role'] != 'admin':
        acc_user = db['users'].find_one({'$or': [{'_id': lead.accepted_by}, {'id': lead.accepted_by}]})
        name = acc_user.get('name') if acc_user else 'another agent'
        raise HTTPException(400, f'This lead has already been accepted by {name}.')

    update_doc = {
        'accepted_by': ctx['user_id'],
        'accepted_at': now(),
        'assigned_to': ctx['user_id']
    }
    if lead.status == 'new':
        update_doc['status'] = 'contacted'
        if lead.external_submission_id:
            try:
                from .mongo import update_lead_in_mongo
                update_lead_in_mongo(lead.external_submission_id, 'contacted')
            except Exception:
                pass

    db['leads'].update_one({'$or': [{'_id': lead_id}, {'id': lead_id}]}, {'$set': update_doc})
    user = db['users'].find_one({'$or': [{'_id': ctx['user_id']}, {'id': ctx['user_id']}]})
    agent_name = user.get('name') if user else 'Agent'
    activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'accepted', f'Lead accepted by {agent_name}')
    audit(db, ctx, 'lead.accepted', lead.id)

    updated = db['leads'].find_one({'$or': [{'_id': lead_id}, {'id': lead_id}]})
    return serialize(updated)


@app.patch('/api/v1/leads/{lead_id}')
def edit_lead(lead_id: str, payload: dict, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, 'leads', lead_id, ctx['organization_id'])
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    unknown = set(payload) - set(LeadInput.model_fields)
    if unknown:
        raise HTTPException(422, 'Unsupported lead fields')
    if 'assigned_to' in payload and payload['assigned_to'] != lead.assigned_to:
        if lead.accepted_by and ctx['role'] != 'admin':
            raise HTTPException(403, 'Once a lead has been accepted, it can only be diverted or reassigned on the Admin side.')

    # Fast-path for quick agent assignment / reassignment by Admin
    if set(payload.keys()) == {'assigned_to'}:
        new_assignee = payload['assigned_to'] or None
        if new_assignee:
            check_assignee(db, ctx['organization_id'], new_assignee)
            assigned_user = db['users'].find_one({'$or': [{'_id': new_assignee}, {'id': new_assignee}]})
            agent_name = assigned_user.get('name') if assigned_user else 'Agent'
            action_desc = f'Assigned to {agent_name} by administrator' if not lead.accepted_by else f'Reassigned to {agent_name} by administrator'
            set_doc = {
                'assigned_to': new_assignee,
                'accepted_by': new_assignee,
                'updated_at': now()
            }
            if not lead.accepted_at:
                set_doc['accepted_at'] = now()
            if lead.status == 'new':
                set_doc['status'] = 'contacted'
                if lead.external_submission_id:
                    try:
                        from .mongo import update_lead_in_mongo
                        update_lead_in_mongo(lead.external_submission_id, 'contacted')
                    except Exception:
                        pass
            db['leads'].update_one({'$or': [{'_id': lead_id}, {'id': lead_id}]}, {'$set': set_doc})
            activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'updated', action_desc)
            audit(db, ctx, 'lead.assigned', lead.id)
        else:
            db['leads'].update_one(
                {'$or': [{'_id': lead_id}, {'id': lead_id}]},
                {'$set': {'assigned_to': None, 'accepted_by': None, 'updated_at': now()}}
            )
            activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'updated', 'Unassigned agent by administrator')
            audit(db, ctx, 'lead.unassigned', lead.id)
        updated = db['leads'].find_one({'$or': [{'_id': lead_id}, {'id': lead_id}]})
        return serialize(updated)

    # Fast-path for quick pipeline stage moves
    if set(payload.keys()) == {'status'}:
        new_status = payload['status']
        if new_status not in ('new', 'contacted', 'qualified', 'converted', 'lost'):
            raise HTTPException(422, 'Invalid status')
        if lead.status != new_status:
            db['leads'].update_one(
                {'$or': [{'_id': lead_id}, {'id': lead_id}]},
                {'$set': {'status': new_status, 'updated_at': now()}}
            )
            activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'updated', f'Stage moved to {new_status}')
            if lead.external_submission_id:
                try:
                    from .mongo import update_lead_in_mongo
                    update_lead_in_mongo(lead.external_submission_id, new_status)
                except Exception:
                    pass
        updated = db['leads'].find_one({'$or': [{'_id': lead_id}, {'id': lead_id}]})
        return serialize(updated)

    values = {key: getattr(lead, key) for key in LeadInput.model_fields}
    if values.get('next_follow_up_at') and values['next_follow_up_at'].tzinfo is None:
        values['next_follow_up_at'] = values['next_follow_up_at'].replace(tzinfo=timezone.utc)
    data = LeadInput(**{**values, **payload})

    check_assignee(db, ctx['organization_id'], data.assigned_to)
    if data.website_id:
        scoped(db, 'websites', data.website_id, ctx['organization_id'])

    changed = []
    set_fields = {}
    for key, value in data.model_dump().items():
        if getattr(lead, key) != value:
            changed.append(key)
            set_fields[key] = value

    if changed:
        if 'assigned_to' in changed:
            if data.assigned_to:
                set_fields['accepted_by'] = data.assigned_to
                if not lead.accepted_at:
                    set_fields['accepted_at'] = now()
                if set_fields.get('status', lead.status) == 'new':
                    set_fields['status'] = 'contacted'
            else:
                set_fields['accepted_by'] = None
        set_fields['updated_at'] = now()
        db['leads'].update_one({'$or': [{'_id': lead_id}, {'id': lead_id}]}, {'$set': set_fields})
        activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'updated', 'Updated: ' + ', '.join(changed))
        if 'status' in changed and lead.external_submission_id:
            try:
                from .mongo import update_lead_in_mongo
                update_lead_in_mongo(lead.external_submission_id, set_fields.get('status', lead.status))
            except Exception:
                pass

        if 'next_follow_up_at' in payload:
            db['follow_ups'].update_many(
                {'organization_id': ctx['organization_id'], 'lead_id': lead.id, 'status': 'open', 'title': 'Scheduled follow-up'},
                {'$set': {'status': 'completed'}}
            )
            if data.next_follow_up_at:
                db['follow_ups'].insert_one(FollowUp(
                    organization_id=ctx['organization_id'],
                    lead_id=lead.id,
                    assigned_to=data.assigned_to,
                    due_at=data.next_follow_up_at,
                    title='Scheduled follow-up'
                ))
            sync_next_follow_up(db, ctx['organization_id'], lead.id)

    updated = db['leads'].find_one({'$or': [{'_id': lead_id}, {'id': lead_id}]})
    return serialize(updated)


@app.delete('/api/v1/leads/{lead_id}')
def delete_lead(lead_id: str, ctx=Depends(admin), db=Depends(get_db)):
    lead = scoped(db, 'leads', lead_id, ctx['organization_id'])
    db['leads'].update_one({'$or': [{'_id': lead_id}, {'id': lead_id}]}, {'$set': {'deleted_at': now()}})
    db['follow_ups'].update_many(
        {'organization_id': ctx['organization_id'], 'lead_id': lead.id},
        {'$set': {'status': 'completed'}}
    )
    audit(db, ctx, 'lead.deleted', lead.id)
    if lead.external_submission_id:
        try:
            from .mongo import update_lead_in_mongo
            update_lead_in_mongo(lead.external_submission_id, 'deleted')
        except Exception:
            pass
    return {'ok': True}


@app.post('/api/v1/leads/{lead_id}/notes', status_code=201)
def note(lead_id: str, payload: NoteInput, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, 'leads', lead_id, ctx['organization_id'])
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    activity(db, ctx['organization_id'], lead_id, ctx['user_id'], 'note', payload.description)
    return {'ok': True}


@app.post('/api/v1/leads/{lead_id}/merge')
def merge(lead_id: str, payload: MergeInput, ctx=Depends(admin), db=Depends(get_db)):
    if lead_id == payload.target_id:
        raise HTTPException(422, 'Choose a different target lead')
    source = scoped(db, 'leads', lead_id, ctx['organization_id'])
    target = scoped(db, 'leads', payload.target_id, ctx['organization_id'])

    set_fields = {}
    for field in ['phone', 'email', 'company_name', 'inquiry_type', 'assigned_to']:
        if not getattr(target, field):
            set_fields[field] = getattr(source, field)
    set_fields['tags'] = list(dict.fromkeys((target.tags or []) + (source.tags or [])))
    set_fields['duplicate_of'] = None
    set_fields['updated_at'] = now()

    db['leads'].update_one({'$or': [{'_id': payload.target_id}, {'id': payload.target_id}]}, {'$set': set_fields})
    db['activities'].update_many(
        {'organization_id': ctx['organization_id'], 'lead_id': source.id},
        {'$set': {'lead_id': target.id}}
    )
    db['follow_ups'].update_many(
        {'organization_id': ctx['organization_id'], 'lead_id': source.id},
        {'$set': {'lead_id': target.id}}
    )
    db['leads'].update_one({'$or': [{'_id': lead_id}, {'id': lead_id}]}, {'$set': {'deleted_at': now()}})
    activity(db, ctx['organization_id'], target.id, ctx['user_id'], 'merged', f'Merged lead {source.id}; existing target fields retained')
    audit(db, ctx, 'lead.merged', f'{source.id}:{target.id}')
    sync_next_follow_up(db, ctx['organization_id'], target.id)

    updated = db['leads'].find_one({'$or': [{'_id': payload.target_id}, {'id': payload.target_id}]})
    return serialize(updated)


@app.post('/api/v1/public/leads')
async def public_lead(request: Request, x_api_key: str = Header(default=''), db=Depends(get_db)):
    limiter.check(('public-ip', request.client.host), 120, 60)
    if len(x_api_key) > 200:
        raise HTTPException(401, 'Invalid integration credential')

    key_hash = digest(x_api_key)
    key_doc = db['website_keys'].find_one({'key_hash': key_hash, 'revoked_at': None})
    if not key_doc:
        raise HTTPException(401, 'Invalid integration credential')
    key = ModelDict(key_doc)

    limiter.check(('website', key.website_id), 60, 60)
    website = scoped(db, 'websites', key.website_id, key.organization_id)
    org_doc = db['organizations'].find_one({'$or': [{'_id': key.organization_id}, {'id': key.organization_id}]})
    if not org_doc or website.status != 'active' or org_doc.get('status') != 'active':
        raise HTTPException(403, 'Integration is paused')

    chunks, length = [], 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > 16384:
            raise HTTPException(413, 'Submission exceeds 16 KB')
        chunks.append(chunk)
    try:
        raw = json.loads(b''.join(chunks))
        if not isinstance(raw, dict):
            raise ValueError()
    except Exception:
        raise HTTPException(422, 'Expected a JSON object')

    if 'organization_id' in raw or 'website_id' in raw or 'assigned_to' in raw:
        raise HTTPException(422, 'Tenant, website and assignment are resolved by the integration')
    if raw.get('company_website'):
        raise HTTPException(422, 'Submission could not be accepted')

    mapping = website.config.get('field_mapping', {}) if website.config else {}
    fields = {k: raw[k] for k in ['full_name', 'phone', 'email', 'company_name', 'inquiry_type'] if k in raw}
    if 'name' in raw and 'full_name' not in fields:
        fields['full_name'] = raw['name']
    fields.update({target: raw[header] for header, target in mapping.items() if header in raw})

    external_id = request.headers.get('Idempotency-Key') or raw.get('external_submission_id')
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 160:
        raise HTTPException(422, 'A stable Idempotency-Key or external_submission_id (1-160 characters) is required')

    attribution = {k: str(raw[k])[:240] for k in ['form_id', 'campaign', 'business_type', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'] if k in raw}
    attribution.setdefault('ingest_channel', 'primary_webhook')

    data = LeadInput(**fields, website_id=website.id, assigned_to=website.config.get('assigned_to') if website.config else None)
    _, replayed = create_lead(db, key.organization_id, data, 'website', external_id=external_id.strip(), attribution=attribution)
    db['audit_logs'].insert_one(AuditLog(
        organization_id=key.organization_id,
        actor_id=None,
        action='submission.replayed' if replayed else 'submission.accepted',
        target=website.id
    ))
    return {'accepted': True, 'channel': 'primary_webhook'}


@app.get('/api/v1/follow-ups')
def follow_ups(status: str = 'open', ctx=Depends(context), db=Depends(get_db)):
    query = {'organization_id': ctx['organization_id']}
    if status:
        query['status'] = status

    cursor = db['follow_ups'].find(query).sort('due_at', 1)
    results = []
    for task in cursor:
        lead = db['leads'].find_one({'$or': [{'_id': task.get('lead_id')}, {'id': task.get('lead_id')}], 'organization_id': ctx['organization_id']})
        if not lead or lead.get('deleted_at'):
            continue
        if ctx['role'] == 'agent' and lead.get('assigned_to') != ctx['user_id']:
            continue
        results.append({**serialize(task), 'lead_name': lead.get('full_name', '')})
    return results


@app.post('/api/v1/follow-ups', status_code=201)
def add_follow_up(payload: FollowUpInput, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, 'leads', payload.lead_id, ctx['organization_id'])
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    check_assignee(db, ctx['organization_id'], payload.assigned_to)

    task = FollowUp(**payload.model_dump(), organization_id=ctx['organization_id'])
    db['follow_ups'].insert_one(task)
    sync_next_follow_up(db, ctx['organization_id'], task.lead_id)
    activity(db, ctx['organization_id'], task.lead_id, ctx['user_id'], 'follow_up', 'Scheduled: ' + task.title)
    return serialize(task)


@app.patch('/api/v1/follow-ups/{task_id}')
def edit_follow_up(task_id: str, payload: FollowUpUpdate, ctx=Depends(writer), db=Depends(get_db)):
    task = scoped(db, 'follow_ups', task_id, ctx['organization_id'])
    if ctx['role'] == 'agent':
        lead = scoped(db, 'leads', task.lead_id, ctx['organization_id'])
        if lead.assigned_to != ctx['user_id']:
            raise HTTPException(403, 'Access to this follow-up is restricted to its assigned agent.')

    db['follow_ups'].update_one({'$or': [{'_id': task_id}, {'id': task_id}]}, {'$set': {'status': payload.status}})
    sync_next_follow_up(db, ctx['organization_id'], task.lead_id)
    activity(db, ctx['organization_id'], task.lead_id, ctx['user_id'], 'follow_up', 'Follow-up ' + payload.status)
    updated = db['follow_ups'].find_one({'$or': [{'_id': task_id}, {'id': task_id}]})
    return serialize(updated)


@app.post('/api/v1/imports/inspect')
async def inspect_import(file: UploadFile = File(...), ctx=Depends(writer)):
    content = await file.read(MAX_BYTES + 1)
    headers, rows, source = parse_file(file.filename or '', content)
    return {'headers': headers, 'samples': rows[:3], 'total_rows': len(rows), 'source': source}


@app.post('/api/v1/imports/preview')
async def preview_import(file: UploadFile = File(...), mapping: str = Form(...), ctx=Depends(writer), db=Depends(get_db)):
    try:
        mapping_obj = json.loads(mapping)
        if not isinstance(mapping_obj, dict):
            raise ValueError()
    except Exception:
        raise HTTPException(422, 'Mapping must be a JSON object')
    job, rows = preview_job(db, ctx, file.filename or '', await file.read(MAX_BYTES + 1), mapping_obj)
    return {**serialize(job), 'rows': rows[:100], 'preview_limit': 100}


@app.post('/api/v1/imports/commit')
def commit_import(payload: CommitInput, ctx=Depends(writer), db=Depends(get_db)):
    job = scoped(db, 'import_jobs', payload.import_job_id, ctx['organization_id'])
    commit_job(db, ctx, job)
    audit(db, ctx, 'import.committed', job.id)
    return serialize(job)


@app.get('/api/v1/imports')
def imports(ctx=Depends(context), db=Depends(get_db)):
    jobs = list(db['import_jobs'].find({'organization_id': ctx['organization_id']}).sort('created_at', -1).limit(100))
    return [serialize(j) for j in jobs]


@app.get('/api/v1/imports/{job_id}/errors')
def import_errors(job_id: str, ctx=Depends(context), db=Depends(get_db)):
    scoped(db, 'import_jobs', job_id, ctx['organization_id'])
    errors = list(db['import_error_rows'].find({'organization_id': ctx['organization_id'], 'import_job_id': job_id}).sort('row_number', 1))
    output = io.StringIO()
    csv_writer = csv.writer(output)
    csv_writer.writerow(['row_number', 'code', 'message'])
    for error in errors:
        csv_writer.writerow([error.get('row_number'), safe_csv_cell(error.get('error_code')), safe_csv_cell(error.get('message'))])
    return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="import-errors.csv"'})


@app.get('/api/v1/dashboard/summary')
def dashboard(website_id: str = '', source: str = '', assigned_to: str = '', date_from: datetime | None = None, date_to: datetime | None = None, ctx=Depends(context), db=Depends(get_db)):
    eff_assigned_to = agent_scope(ctx) or assigned_to
    query = build_lead_filter(ctx, website_id=website_id, source=source, assigned_to=eff_assigned_to, date_from=date_from, date_to=date_to)
    leads_cursor = db['leads'].find(query)
    rows = list(leads_cursor)

    status_counts = {s: sum(l.get('status') == s for l in rows) for s in ['new', 'contacted', 'qualified', 'converted', 'lost']}

    from zoneinfo import ZoneInfo
    local_now = now().astimezone(ZoneInfo('Asia/Kolkata'))
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = start + timedelta(days=1)

    lead_ids = [l.get('id') or str(l.get('_id')) for l in rows]
    task_query = {
        'organization_id': ctx['organization_id'],
        'status': 'open',
        'lead_id': {'$in': lead_ids}
    }
    tasks = list(db['follow_ups'].find(task_query))
    dates = []
    for t in tasks:
        val = t.get('due_at')
        if val:
            if getattr(val, 'tzinfo', None) is None:
                val = val.replace(tzinfo=timezone.utc)
            else:
                val = val.astimezone(timezone.utc)
            dates.append(val)

    sources = {s: sum(l.get('source') == s for l in rows) for s in ['manual', 'website', 'csv', 'xlsx']}
    sites = list(db['websites'].find({'organization_id': ctx['organization_id']}))

    unaccepted_query = {
        'organization_id': ctx['organization_id'],
        'deleted_at': None,
        'accepted_by': None
    }
    unaccepted = list(db['leads'].find(unaccepted_query).sort('created_at', -1).limit(10))
    unaccepted_count = db['leads'].count_documents(unaccepted_query)

    unaccepted_leads = [
        {
            'id': l.get('id') or str(l.get('_id')),
            'full_name': l.get('full_name'),
            'company_name': l.get('company_name'),
            'source': l.get('source'),
            'created_at': l['created_at'].isoformat() if l.get('created_at') else None
        }
        for l in unaccepted
    ]

    return {
        'total': len(rows),
        'new_in_period': len(rows),
        'uncontacted': status_counts['new'],
        'unaccepted_count': unaccepted_count,
        'unaccepted_leads': unaccepted_leads,
        'statuses': status_counts,
        'conversion_rate': round(status_counts['converted'] / len(rows) * 100, 1) if rows else 0,
        'due_today': sum(start <= d < end for d in dates),
        'overdue': sum(d < now() for d in dates),
        'sources': sources,
        'websites': [{'id': w.get('id') or str(w.get('_id')), 'name': w.get('name'), 'count': sum(l.get('website_id') == (w.get('id') or str(w.get('_id'))) for l in rows)} for w in sites],
        'cohort': 'Leads created within the selected date range',
        'timezone': 'Asia/Kolkata'
    }


@app.get('/api/v1/audit-logs')
def audit_logs(ctx=Depends(admin), db=Depends(get_db)):
    logs = list(db['audit_logs'].find({'organization_id': ctx['organization_id']}).sort('created_at', -1).limit(100))
    return [serialize(a) for a in logs]


@app.delete('/api/v1/audit-logs')
def clear_audit_logs(ctx=Depends(admin), db=Depends(get_db)):
    db['audit_logs'].delete_many({'organization_id': ctx['organization_id']})
    return {'ok': True, 'message': 'All audit logs cleared'}


@app.delete('/api/v1/audit-logs/{log_id}')
def delete_audit_log(log_id: str, ctx=Depends(admin), db=Depends(get_db)):
    db['audit_logs'].delete_one({
        '$and': [
            {'$or': [{'_id': log_id}, {'id': log_id}]},
            {'organization_id': ctx['organization_id']}
        ]
    })
    return {'ok': True}

