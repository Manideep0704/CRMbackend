import asyncio
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .db import get_db, now
from .imports import MAX_BYTES, commit_job, parse_file, preview_job, safe_csv_cell
from .models import Activity, AuditLog, AuthSession, FollowUp, ImportErrorRow, ImportJob, Lead, Membership, Organization, User, Website, WebsiteKey
from .schemas import CommitInput, FollowUpInput, FollowUpUpdate, LeadInput, Login, MembershipInput, MergeInput, NoteInput, WebsiteInput
from .security import DUMMY_PASSWORD, admin, context, digest, hash_password, is_trusted_origin, limiter, new_session, revoke_user_sessions, verify_password, writer
from .services import activity, audit, check_assignee, create_lead, scoped, serialize, sync_next_follow_up

app = FastAPI(title='Timely CRM', version='1.0.0')
# CORS: exact origin allowlist only; no wildcard regex.  Configure FRONTEND_ORIGIN in env.
_CORS_ORIGINS = [
    o.strip().rstrip('/') for o in
    os.getenv('FRONTEND_ORIGIN', 'http://localhost:3000,http://127.0.0.1:3000').split(',')
    if o.strip() and o.strip() != '*'
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'],
    allow_headers=['Content-Type', 'X-Timely-Request', 'X-Api-Key'],
)


@app.on_event("startup")
def startup_db_init():
    from .db import Base, engine, SessionLocal
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        org = db.scalar(select(Organization).where(Organization.slug == "timely-health"))
        if not org:
            org = Organization(name="Timely Health", slug="timely-health", status="active")
            db.add(org)
            db.flush()
        website = db.scalar(select(Website).where(Website.organization_id == org.id, Website.domain == "digital.timelyhealth.in"))
        if not website:
            website = Website(
                organization_id=org.id,
                name="Timely Health Digital",
                domain="digital.timelyhealth.in",
                status="active",
                config={}
            )
            db.add(website)
            db.flush()

        # Controlled initial administrator provisioning via environment variables only (S1)
        init_admin_email = os.getenv("INITIAL_ADMIN_EMAIL")
        init_admin_pass = os.getenv("INITIAL_ADMIN_PASSWORD")
        if init_admin_email and init_admin_pass:
            user = db.scalar(select(User).where(User.email == init_admin_email.lower()))
            if not user:
                user = User(
                    name="Administrator",
                    email=init_admin_email.lower(),
                    password_hash=hash_password(init_admin_pass),
                    is_active=True,
                    auth_provider='local',
                    token_version=1
                )
                db.add(user)
                db.flush()
            if not db.get(Membership, (org.id, user.id)):
                db.add(Membership(organization_id=org.id, user_id=user.id, role="admin", status="active"))
                db.flush()

        # Controlled initial website API key provisioning via environment variable only (S1)
        init_key = os.getenv("INITIAL_WEBSITE_KEY")
        if init_key and website:
            key_hash = digest(init_key)
            key = db.scalar(select(WebsiteKey).where(WebsiteKey.website_id == website.id, WebsiteKey.key_hash == key_hash))
            if not key:
                db.add(WebsiteKey(
                    organization_id=org.id,
                    website_id=website.id,
                    key_hash=key_hash
                ))
        db.commit()
    try:
        from .mongo import sync_sales_agents_from_mongo
        sync_sales_agents_from_mongo()
    except Exception as e:
        print(f"[Sales Sync Startup Error]: {e}")
    asyncio.create_task(sync_website_leads_fallback_worker())


# ==============================================================================
# FALLBACK RECONCILIATION WORKER
# Webhook (/api/v1/public/leads) is the Primary Channel (0-second push).
# This background worker is the Fallback Channel: every 30s it reconciles
# with api.timelyhealth.in to automatically catch and recover any missed leads.
# ==============================================================================
async def sync_website_leads_fallback_worker():
    await asyncio.sleep(5)
    while True:
        try:
            # Direct Company MongoDB Sync
            try:
                from .mongo import sync_leads_from_mongo, sync_sales_agents_from_mongo
                sync_leads_from_mongo()
                sync_sales_agents_from_mongo()
            except Exception as e:
                pass

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get('https://api.timelyhealth.in/api/consultation-leads?limit=50')
                if res.status_code == 200:
                    data = res.json().get('data', [])
                    from .db import SessionLocal
                    with SessionLocal() as db:
                        org = db.scalar(select(Organization).where(Organization.slug == 'timely-health'))
                        website = db.scalar(select(Website).where(Website.domain == 'digital.timelyhealth.in'))
                        if org and website:
                            recovered = 0
                            for item in data:
                                ext_id = f"timely-consultation:{item['_id']}"
                                existing = db.scalar(select(Lead).where(
                                    Lead.organization_id == org.id,
                                    Lead.external_submission_id == ext_id
                                ))
                                if not existing:
                                    lead_input = LeadInput(
                                        full_name=item.get('name') or 'Website Lead',
                                        email=item.get('email') or '',
                                        phone=item.get('phone') or '',
                                        company_name=item.get('businessName') or '',
                                        inquiry_type=item.get('lookingFor') or 'Digital consultation',
                                        website_id=website.id
                                    )
                                    attribution = {
                                        'business_type': item.get('businessType') or '',
                                        'form_id': 'timely-digital-consultation',
                                        'ingest_channel': 'fallback_sync'
                                    }
                                    create_lead(db, org.id, lead_input, 'website', external_id=ext_id, attribution=attribution)
                                    recovered += 1
                            if recovered > 0:
                                db.commit()
                                print(f"[CRM Fallback Sync] Recovered {recovered} lead(s) missed by primary webhook.")
        except Exception:
            pass
        await asyncio.sleep(30)


@app.get('/api/v1/public/ingest-status')
def ingest_channel_status():
    return {
        'status': 'healthy',
        'primary': {
            'type': 'webhook',
            'endpoint': '/api/v1/public/leads',
            'role': 'main_instant_push'
        },
        'fallback': {
            'type': 'background_reconciliation_sync',
            'target': 'https://api.timelyhealth.in/api/consultation-leads',
            'interval_seconds': 30,
            'role': 'automated_safety_net'
        }
    }


# S6 – Removed unauthenticated /init-db endpoint.  Run `python -m app.seed` for provisioning.


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    # Never reflect passwords or arbitrary uploaded contact data in errors.
    return JSONResponse(status_code=422, content={'detail': [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in error.errors()]})


@app.exception_handler(IntegrityError)
async def integrity_error(request, error):
    return JSONResponse(status_code=409, content={'detail': 'Record conflicts with an existing record or tenant relationship. Refresh and retry.'})


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@app.get('/health')
def health(db=Depends(get_db)):
    db.execute(select(1))
    return {'status': 'ok', 'service': 'Timely CRM'}


@app.post('/api/v1/auth/login')
def login(payload: Login, request: Request, response: Response, db=Depends(get_db)):
    # S8 – tighter login bucket: 5 attempts per 5 minutes per IP
    limiter.check(('login', request.client.host), 5, 300)
    origin = request.headers.get('origin')
    if request.headers.get('X-Timely-Request') != '1' or not is_trusted_origin(origin):
        raise HTTPException(403, 'Untrusted sign-in request')

    # S2 – Reject unknown or inactive organization slugs; do NOT silently fall back.
    org = db.scalar(select(Organization).where(Organization.slug == payload.organization_slug, Organization.status == 'active'))
    if not org:
        raise HTTPException(401, 'Invalid email, password or organization')

    user = db.scalar(select(User).where(User.email == payload.email.lower()))

    # S3/R4 – Distinguish local vs federated accounts.
    # Persist auth_provider: if account is federated ('attendance'), authentication MUST delegate to Mongo.
    # Provider error/absence MUST fail closed (never fall back to local password).
    from .mongo import authenticate_attendance_user, check_attendance_user_status
    is_local_user = bool(user and getattr(user, 'auth_provider', None) == 'local')
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
            # S3/R4 – Directory outage must NEVER fall back to local auth; fail closed.
            raise HTTPException(503, 'External authentication provider is temporarily unavailable. Please try again later.')
        mongo_user = authenticate_attendance_user(payload.email, payload.password)
        valid = mongo_user is not None
        # S3 – Wrong password does NOT globally disable user! Only invalidates this attempt.
    else:
        # Strictly local account: only verify against local scrypt hash if NOT federated
        valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD)

    if mongo_user:
        incoming_sub = str(mongo_user.get('_id', '')).strip()
        incoming_email = mongo_user['email'].lower().strip()

        # R5 – Subject-first identity resolution and collision enforcement
        user_by_sub = db.scalar(
            select(User).where(
                User.auth_provider == 'attendance',
                User.auth_subject == incoming_sub
            )
        ) if incoming_sub else None

        user_by_email = db.scalar(
            select(User).where(User.email == incoming_email)
        )

        if user_by_sub and user_by_email and user_by_sub.id != user_by_email.id:
            raise HTTPException(403, 'Identity collision: external subject and email belong to different accounts.')

        if user_by_sub:
            user = user_by_sub
            if user.email != incoming_email:
                user.email = incoming_email
        elif user_by_email:
            if user_by_email.auth_provider == 'local':
                raise HTTPException(403, 'A local account already exists with this email. Federated linking requires administrator action.')
            if user_by_email.auth_subject and user_by_email.auth_subject != incoming_sub:
                raise HTTPException(403, 'External identity subject mismatch for this account.')
            user = user_by_email
            user.auth_provider = 'attendance'
            user.auth_subject = incoming_sub
        else:
            user = User(
                name=mongo_user.get('name') or 'User',
                email=incoming_email,
                # S3 – Random unguessable hash for federated users; auth is always delegated to Mongo
                password_hash=hash_password(secrets.token_urlsafe(32)),
                is_active=True,
                auth_provider='attendance',
                auth_subject=incoming_sub,
                token_version=1
            )
            db.add(user)
            db.flush()

        # R6 – Local suspension policy: CRM administrator suspension strictly overrides external provider status
        if not user.is_active:
            raise HTTPException(403, 'Account is inactive or suspended in CRM.')

        # S3 – Overwrite any legacy password hash with random unguessable secret
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        if mongo_user.get('name') and not user.name:
            user.name = mongo_user['name']
        db.flush()

        # S2 – Check organization entitlement!
        # An attendance identity is ONLY entitled to their designated organization (e.g. 'timely-health').
        # Cross-tenant auto-provisioning into arbitrary requested organizations is strictly forbidden.
        entitled_slug = mongo_user.get('entitled_org_slug', 'timely-health')
        membership_check = db.get(Membership, (org.id, user.id))
        if not membership_check:
            # Deny provisioning in unentitled organizations
            if org.slug != entitled_slug:
                raise HTTPException(401, 'Invalid email, password or organization')

            allowed_roles = {'admin', 'agent', 'viewer'}
            raw_role = mongo_user.get('role', 'viewer')
            safe_role = raw_role if raw_role in allowed_roles else 'viewer'
            membership_check = Membership(
                organization_id=org.id,
                user_id=user.id,
                role=safe_role,
                status='active'
            )
            db.add(membership_check)
            db.flush()
        else:
            if membership_check.status != 'active':
                raise HTTPException(403, 'Organization access is inactive')
            if org.slug == entitled_slug and mongo_user.get('role'):
                allowed_roles = {'admin', 'agent', 'viewer'}
                raw_role = mongo_user.get('role', membership_check.role)
                if raw_role in allowed_roles:
                    membership_check.role = raw_role
                    db.flush()
        valid = True

    membership = db.get(Membership, (org.id, user.id)) if org and user else None
    if not valid or not user or not user.is_active or not membership or membership.status != 'active':
        raise HTTPException(401, 'Invalid email, password or organization')
    token = new_session(db, user, org.id)
    db.commit()
    # S5 – Secure flag: always True in production regardless of origin.
    is_production = os.getenv('APP_ENV') == 'production'
    is_secure = is_production
    response.set_cookie('timely_session', token, httponly=True, secure=is_secure, samesite='lax', max_age=43200, path='/')
    return {'user': serialize(user), 'organization': serialize(org), 'role': membership.role}


@app.post('/api/v1/auth/logout')
def logout(request: Request, response: Response, ctx=Depends(context), db=Depends(get_db)):
    token = request.cookies.get('timely_session', '')
    session = db.get(AuthSession, digest(token))
    if session:
        db.delete(session)
    db.commit()
    response.delete_cookie('timely_session', path='/')
    return {'ok': True}


@app.post('/api/v1/auth/revoke-all')
def revoke_all(response: Response, ctx=Depends(context), db=Depends(get_db)):
    # S5 – Revoke all active sessions for current user
    revoke_user_sessions(db, ctx['user_id'])
    db.commit()
    response.delete_cookie('timely_session', path='/')
    return {'ok': True, 'message': 'All active sessions revoked'}


@app.get('/api/v1/organizations/me')
def me(ctx=Depends(context)):
    return {'user': serialize(ctx['user']), 'organization': serialize(ctx['organization']), 'role': ctx['role']}


@app.get('/api/v1/members')
def members(ctx=Depends(context), db=Depends(get_db)):
    rows = db.execute(select(User, Membership).join(Membership, User.id == Membership.user_id).where(Membership.organization_id == ctx['organization_id'])).all()
    return [{**serialize(user), 'role': member.role, 'membership_status': member.status} for user, member in rows]


@app.post('/api/v1/members', status_code=201)
def add_member(payload: MembershipInput, ctx=Depends(admin), db=Depends(get_db)):
    # Existing global identities cannot be attached or reset through this endpoint.
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(409, 'Email is already registered. Existing-user invitations are outside this MVP.')
    user = User(name=payload.name, email=payload.email.lower(), password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    db.add(Membership(organization_id=ctx['organization_id'], user_id=user.id, role=payload.role))
    audit(db, ctx, 'member.created', user.id)
    db.commit()
    return serialize(user)


@app.get('/api/v1/websites')
def websites(ctx=Depends(context), db=Depends(get_db)):
    return [serialize(w) for w in db.scalars(select(Website).where(Website.organization_id == ctx['organization_id']).order_by(Website.created_at))]


@app.post('/api/v1/websites', status_code=201)
def add_website(payload: WebsiteInput, ctx=Depends(admin), db=Depends(get_db)):
    check_assignee(db, ctx['organization_id'], payload.assigned_to)
    website = Website(organization_id=ctx['organization_id'], name=payload.name, domain=payload.domain, status=payload.status, config={'field_mapping': payload.field_mapping, 'assigned_to': payload.assigned_to})
    db.add(website)
    db.flush()
    audit(db, ctx, 'website.created', website.id)
    db.commit()
    return serialize(website)


@app.patch('/api/v1/websites/{website_id}')
def edit_website(website_id: str, payload: WebsiteInput, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, Website, website_id, ctx['organization_id'])
    check_assignee(db, ctx['organization_id'], payload.assigned_to)
    website.name, website.domain, website.status = payload.name, payload.domain, payload.status
    website.config = {'field_mapping': payload.field_mapping, 'assigned_to': payload.assigned_to}
    audit(db, ctx, 'website.updated', website.id)
    db.commit()
    return serialize(website)


@app.post('/api/v1/websites/{website_id}/rotate-key')
def rotate_key(website_id: str, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, Website, website_id, ctx['organization_id'], lock=True)
    db.execute(update(WebsiteKey).where(WebsiteKey.website_id == website.id, WebsiteKey.revoked_at.is_(None)).values(revoked_at=now()))
    token = 'tly_' + secrets.token_urlsafe(40)
    db.add(WebsiteKey(organization_id=ctx['organization_id'], website_id=website.id, key_hash=digest(token)))
    audit(db, ctx, 'website.key_rotated', website.id)
    db.commit()
    return {'key': token, 'message': 'Shown once. Store only in your website server environment. Old keys are revoked.'}


@app.delete('/api/v1/websites/{website_id}/key')
def revoke_key(website_id: str, ctx=Depends(admin), db=Depends(get_db)):
    website = scoped(db, Website, website_id, ctx['organization_id'], lock=True)
    db.execute(update(WebsiteKey).where(WebsiteKey.website_id == website.id, WebsiteKey.revoked_at.is_(None)).values(revoked_at=now()))
    audit(db, ctx, 'website.key_revoked', website.id)
    db.commit()
    return {'ok': True}


def agent_scope(ctx) -> str | None:
    """S4 – Returns the user_id to scope leads to for agent role, or None for admin/viewer.
    Agents always see only their own assigned leads across ALL endpoints.
    This is the single authoritative place to enforce that policy."""
    return ctx['user_id'] if ctx['role'] == 'agent' else None


def lead_query(ctx, q='', status='', website_id='', source='', assigned_to='', date_from=None, date_to=None):
    query = select(Lead).where(Lead.organization_id == ctx['organization_id'], Lead.deleted_at.is_(None))
    if q:
        pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.where(or_(*[column.ilike(pattern, escape='\\') for column in [Lead.full_name, Lead.email, Lead.phone, Lead.company_name]]))
    for column, value in [(Lead.status, status), (Lead.website_id, website_id), (Lead.source, source), (Lead.assigned_to, assigned_to)]:
        if value:
            query = query.where(column == value)
    if date_from:
        if date_from.tzinfo is None:
            raise HTTPException(422, 'Date filters must include a timezone')
        query = query.where(Lead.created_at >= date_from.astimezone(timezone.utc))
    if date_to:
        if date_to.tzinfo is None:
            raise HTTPException(422, 'Date filters must include a timezone')
        query = query.where(Lead.created_at <= date_to.astimezone(timezone.utc))
    return query


@app.get('/api/v1/leads')
def leads(q: str = '', status: str = '', website_id: str = '', source: str = '', assigned_to: str = '', unclaimed: bool = False, pipeline: bool = False, date_from: datetime | None = None, date_to: datetime | None = None, page: int = 1, page_size: int = 20, sort: str = 'newest', ctx=Depends(context), db=Depends(get_db)):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, 'Invalid pagination')
    if unclaimed:
        # S4 – Unclaimed shared queue: leads awaiting claim/acceptance across the tenant
        query = select(Lead).where(
            Lead.organization_id == ctx['organization_id'],
            Lead.deleted_at.is_(None),
            Lead.accepted_by.is_(None)
        )
        if q:
            pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            query = query.where(or_(*[column.ilike(pattern, escape='\\') for column in [Lead.full_name, Lead.email, Lead.phone, Lead.company_name]]))
        for column, value in [(Lead.status, status), (Lead.website_id, website_id), (Lead.source, source)]:
            if value:
                query = query.where(column == value)
        if date_from:
            if date_from.tzinfo is None:
                raise HTTPException(422, 'Date filters must include a timezone')
            query = query.where(Lead.created_at >= date_from.astimezone(timezone.utc))
        if date_to:
            if date_to.tzinfo is None:
                raise HTTPException(422, 'Date filters must include a timezone')
            query = query.where(Lead.created_at <= date_to.astimezone(timezone.utc))
    else:
        # S4 – agent scope applied always, not just in pipeline mode.
        # agent_scope() returns the agent's own user_id; admin/viewer get None (no restriction).
        eff_assigned_to = agent_scope(ctx) or assigned_to
        query = lead_query(ctx, q, status, website_id, source, eff_assigned_to, date_from, date_to)

    count = db.scalar(select(func.count()).select_from(query.subquery()))
    order = {'newest': Lead.created_at.desc(), 'oldest': Lead.created_at.asc(), 'name': Lead.full_name.asc()}.get(sort)
    if order is None:
        raise HTTPException(422, 'Sort must be newest, oldest or name')
    return {'items': [serialize(l) for l in db.scalars(query.order_by(order, Lead.id).offset((page - 1) * page_size).limit(page_size))], 'total': count, 'page': page, 'page_size': page_size}


@app.get('/api/v1/leads/queue')
def leads_queue(q: str = '', status: str = '', website_id: str = '', source: str = '', page: int = 1, page_size: int = 20, sort: str = 'newest', ctx=Depends(context), db=Depends(get_db)):
    """
    S4 – Dedicated paginated claim inbox for unaccepted leads awaiting assignment.
    Accessible to all active tenant members (agents & admins).
    """
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
    db.commit()
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
    query = lead_query(ctx, q=q, status=status, website_id=website_id, source=source, date_from=date_from, date_to=date_to)
    leads = db.scalars(query.order_by(Lead.created_at.desc())).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'S.No', 'Lead ID', 'Full Name', 'Phone', 'Email',
        'Company / Clinic', 'Inquiry / Looking For', 'Status',
        'Priority', 'Source', 'Website', 'Assigned To',
        'Next Follow-Up', 'Created Date'
    ])
    websites_dict = {w.id: w.name for w in db.scalars(select(Website).where(Website.organization_id == ctx['organization_id']))}
    members_dict = {u.id: u.name for u in db.scalars(select(User).join(Membership, (Membership.user_id == User.id) & (Membership.organization_id == ctx['organization_id'])))}

    for idx, lead in enumerate(leads, 1):
        raw_p = (lead.phone or '').replace("'", "").strip()
        p_digits = re.sub(r'\D', '', raw_p)
        if len(p_digits) == 12 and p_digits.startswith('91'):
            clean_phone = p_digits[2:]
        elif len(p_digits) == 11 and p_digits.startswith('0'):
            clean_phone = p_digits[1:]
        elif len(p_digits) == 10:
            clean_phone = p_digits
        else:
            clean_phone = p_digits or raw_p
        formatted_source = 'Website Form' if lead.source == 'website' else 'Manual Entry' if lead.source == 'manual' else (lead.source or '').upper()
        next_follow = '-'
        if lead.next_follow_up_at:
            dt_fu = lead.next_follow_up_at if lead.next_follow_up_at.tzinfo else lead.next_follow_up_at.replace(tzinfo=timezone.utc)
            next_follow = dt_fu.astimezone(ist).strftime('%d-%b-%Y %I:%M %p')

        created_str = '-'
        if lead.created_at:
            dt_cr = lead.created_at if lead.created_at.tzinfo else lead.created_at.replace(tzinfo=timezone.utc)
            created_str = dt_cr.astimezone(ist).strftime('%d-%b-%Y %I:%M %p')

        # S7 – Sanitize every string cell to neutralise spreadsheet formula injection.
        writer.writerow([
            idx,
            safe_csv_cell(f"TL-{lead.id[:8].upper()}"),
            safe_csv_cell(lead.full_name or ''),
            safe_csv_cell(clean_phone),
            safe_csv_cell(lead.email or ''),
            safe_csv_cell(lead.company_name or ''),
            safe_csv_cell(lead.inquiry_type or ''),
            safe_csv_cell((lead.status or '').capitalize()),
            safe_csv_cell((lead.priority or '').capitalize()),
            safe_csv_cell(formatted_source),
            safe_csv_cell(websites_dict.get(lead.website_id, 'Timely Health Digital')),
            safe_csv_cell(members_dict.get(lead.assigned_to, 'Unassigned')),
            safe_csv_cell(next_follow),
            safe_csv_cell(created_str),
        ])

    # S8/P1 – Commit the audit record so it persists even if the caller disconnects.
    audit(db, ctx, 'leads.exported', f'count:{len(leads)}')
    db.commit()
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
    lead = scoped(db, Lead, lead_id, ctx['organization_id'])
    # S4 – agents may only read leads they are assigned to, or unaccepted leads awaiting claim.
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id'] and lead.accepted_by is not None:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    events = db.scalars(select(Activity).where(Activity.organization_id == ctx['organization_id'], Activity.lead_id == lead.id).order_by(Activity.created_at.desc()))
    return {**serialize(lead), 'activities': [serialize(e) for e in events]}


@app.post('/api/v1/leads/{lead_id}/accept')
def accept_lead(lead_id: str, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, Lead, lead_id, ctx['organization_id'], lock=True)
    if lead.accepted_by:
        accepted_user = db.get(User, lead.accepted_by)
        name = accepted_user.name if accepted_user else 'another agent'
        raise HTTPException(400, f'This lead has already been accepted by {name}.')
    
    lead.accepted_by = ctx['user_id']
    lead.accepted_at = now()
    lead.assigned_to = ctx['user_id']
    if lead.status == 'new':
        lead.status = 'contacted'
        if lead.external_submission_id:
            try:
                from .mongo import update_lead_in_mongo
                update_lead_in_mongo(lead.external_submission_id, lead.status)
            except Exception:
                pass

    user = db.get(User, ctx['user_id'])
    agent_name = user.name if user else 'Agent'
    activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'accepted', f'Lead accepted by {agent_name}')
    audit(db, ctx, 'lead.accepted', lead.id)
    db.commit()
    return serialize(lead)


@app.patch('/api/v1/leads/{lead_id}')
def edit_lead(lead_id: str, payload: dict, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, Lead, lead_id, ctx['organization_id'], lock=True)
    # S4 – agents may only edit leads assigned to them.
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    unknown = set(payload) - set(LeadInput.model_fields)
    if unknown:
        raise HTTPException(422, 'Unsupported lead fields')
    # If diverting or reassigning an already accepted lead, only Admins are authorized
    if 'assigned_to' in payload and payload['assigned_to'] != lead.assigned_to:
        if lead.accepted_by and ctx['role'] != 'admin':
            raise HTTPException(403, 'Once a lead has been accepted, it can only be diverted or reassigned on the Admin side.')
    from pydantic import ValidationError
    try:
        values = {key: getattr(lead, key) for key in LeadInput.model_fields}
        if values['next_follow_up_at'] and values['next_follow_up_at'].tzinfo is None:
            values['next_follow_up_at'] = values['next_follow_up_at'].replace(tzinfo=timezone.utc)
        data = LeadInput(**{**values, **payload})
    except ValidationError as error:
        raise HTTPException(422, [{'loc': e['loc'], 'msg': e['msg']} for e in error.errors(include_input=False, include_url=False)])
    check_assignee(db, ctx['organization_id'], data.assigned_to)
    if data.website_id:
        scoped(db, Website, data.website_id, ctx['organization_id'])
    changed = []
    for key, value in data.model_dump().items():
        if getattr(lead, key) != value:
            changed.append(key)
            setattr(lead, key, value)
    if changed:
        activity(db, ctx['organization_id'], lead.id, ctx['user_id'], 'updated', 'Updated: ' + ', '.join(changed))
        if 'status' in changed and lead.external_submission_id:
            try:
                from .mongo import update_lead_in_mongo
                update_lead_in_mongo(lead.external_submission_id, lead.status)
            except Exception:
                pass
        if 'next_follow_up_at' in payload:
            tasks = db.scalars(select(FollowUp).where(FollowUp.organization_id == ctx['organization_id'], FollowUp.lead_id == lead.id, FollowUp.status == 'open', FollowUp.title == 'Scheduled follow-up')).all()
            for task in tasks:
                task.status = 'completed'
            if data.next_follow_up_at:
                db.add(FollowUp(organization_id=ctx['organization_id'], lead_id=lead.id, assigned_to=data.assigned_to, due_at=data.next_follow_up_at, title='Scheduled follow-up'))
            db.flush()
            sync_next_follow_up(db, ctx['organization_id'], lead.id)
    db.commit()
    return serialize(lead)


@app.delete('/api/v1/leads/{lead_id}')
def delete_lead(lead_id: str, ctx=Depends(admin), db=Depends(get_db)):
    lead = scoped(db, Lead, lead_id, ctx['organization_id'], lock=True)
    lead.deleted_at = now()
    db.execute(update(FollowUp).where(FollowUp.organization_id == ctx['organization_id'], FollowUp.lead_id == lead.id).values(status='completed'))
    audit(db, ctx, 'lead.deleted', lead.id)
    if lead.external_submission_id:
        try:
            from .mongo import update_lead_in_mongo
            update_lead_in_mongo(lead.external_submission_id, 'deleted')
        except Exception:
            pass
    db.commit()
    return {'ok': True}


@app.post('/api/v1/leads/{lead_id}/notes', status_code=201)
def note(lead_id: str, payload: NoteInput, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, Lead, lead_id, ctx['organization_id'])
    # S4 – agents may only add notes to leads assigned to them.
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    activity(db, ctx['organization_id'], lead_id, ctx['user_id'], 'note', payload.description)
    db.commit()
    return {'ok': True}


@app.post('/api/v1/leads/{lead_id}/merge')
def merge(lead_id: str, payload: MergeInput, ctx=Depends(admin), db=Depends(get_db)):
    if lead_id == payload.target_id:
        raise HTTPException(422, 'Choose a different target lead')
    # Deterministic lock order avoids opposite-direction merge deadlocks.
    for item_id in sorted([lead_id, payload.target_id]):
        scoped(db, Lead, item_id, ctx['organization_id'], lock=True)
    source = scoped(db, Lead, lead_id, ctx['organization_id'])
    target = scoped(db, Lead, payload.target_id, ctx['organization_id'])
    for field in ['phone', 'email', 'company_name', 'inquiry_type', 'assigned_to']:
        if not getattr(target, field):
            setattr(target, field, getattr(source, field))
    target.tags = list(dict.fromkeys(target.tags + source.tags))
    db.execute(update(Activity).where(Activity.organization_id == ctx['organization_id'], Activity.lead_id == source.id).values(lead_id=target.id))
    db.execute(update(FollowUp).where(FollowUp.organization_id == ctx['organization_id'], FollowUp.lead_id == source.id).values(lead_id=target.id))
    source.deleted_at = now()
    target.duplicate_of = None
    activity(db, ctx['organization_id'], target.id, ctx['user_id'], 'merged', f'Merged lead {source.id}; existing target fields retained')
    audit(db, ctx, 'lead.merged', f'{source.id}:{target.id}')
    db.flush()
    sync_next_follow_up(db, ctx['organization_id'], target.id)
    db.commit()
    return serialize(target)


@app.post('/api/v1/public/leads')
async def public_lead(request: Request, x_api_key: str = Header(default=''), db=Depends(get_db)):
    limiter.check(('public-ip', request.client.host), 120, 60)
    if len(x_api_key) > 200:
        raise HTTPException(401, 'Invalid integration credential')
    key = db.scalar(select(WebsiteKey).where(WebsiteKey.key_hash == digest(x_api_key), WebsiteKey.revoked_at.is_(None)))
    if not key:
        raise HTTPException(401, 'Invalid integration credential')
    limiter.check(('website', key.website_id), 60, 60)
    website = scoped(db, Website, key.website_id, key.organization_id)
    org = db.get(Organization, key.organization_id)
    if website.status != 'active' or org.status != 'active':
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
    mapping = website.config.get('field_mapping', {})
    fields = {k: raw[k] for k in ['full_name', 'phone', 'email', 'company_name', 'inquiry_type'] if k in raw}
    if 'name' in raw and 'full_name' not in fields:
        fields['full_name'] = raw['name']
    fields.update({target: raw[header] for header, target in mapping.items() if header in raw})
    external_id = request.headers.get('Idempotency-Key') or raw.get('external_submission_id')
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 160:
        raise HTTPException(422, 'A stable Idempotency-Key or external_submission_id (1-160 characters) is required')
    attribution = {k: str(raw[k])[:240] for k in ['form_id', 'campaign', 'business_type', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'] if k in raw}
    attribution.setdefault('ingest_channel', 'primary_webhook')
    from pydantic import ValidationError
    try:
        data = LeadInput(**fields, website_id=website.id, assigned_to=website.config.get('assigned_to'))
    except ValidationError as error:
        raise HTTPException(422, [{'loc': e['loc'], 'msg': e['msg']} for e in error.errors(include_input=False, include_url=False)])
    try:
        _, replayed = create_lead(db, key.organization_id, data, 'website', external_id=external_id.strip(), attribution=attribution)
        db.add(AuditLog(organization_id=key.organization_id, action='submission.replayed' if replayed else 'submission.accepted', target=website.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.scalar(select(Lead).where(Lead.organization_id == key.organization_id, Lead.website_id == website.id, Lead.external_submission_id == external_id.strip()))
        if not prior:
            raise
    # A stable response does not reveal whether a contact already exists.
    return {'accepted': True, 'channel': 'primary_webhook'}


@app.get('/api/v1/follow-ups')
def follow_ups(status: str = 'open', ctx=Depends(context), db=Depends(get_db)):
    query = select(FollowUp, Lead.full_name).join(Lead, (Lead.id == FollowUp.lead_id) & (Lead.organization_id == FollowUp.organization_id)).where(FollowUp.organization_id == ctx['organization_id'], Lead.deleted_at.is_(None))
    if status:
        query = query.where(FollowUp.status == status)
    # S4 – agents see only follow-ups for leads assigned to them.
    if ctx['role'] == 'agent':
        query = query.where(Lead.assigned_to == ctx['user_id'])
    return [{**serialize(task), 'lead_name': name} for task, name in db.execute(query.order_by(FollowUp.due_at))]


@app.post('/api/v1/follow-ups', status_code=201)
def add_follow_up(payload: FollowUpInput, ctx=Depends(writer), db=Depends(get_db)):
    lead = scoped(db, Lead, payload.lead_id, ctx['organization_id'])
    # S4 – agents may only schedule follow-ups for leads assigned to them.
    if ctx['role'] == 'agent' and lead.assigned_to != ctx['user_id']:
        raise HTTPException(403, 'Access to this lead is restricted to its assigned agent.')
    check_assignee(db, ctx['organization_id'], payload.assigned_to)
    task = FollowUp(**payload.model_dump(), organization_id=ctx['organization_id'])
    db.add(task)
    db.flush()
    sync_next_follow_up(db, ctx['organization_id'], task.lead_id)
    activity(db, ctx['organization_id'], task.lead_id, ctx['user_id'], 'follow_up', 'Scheduled: ' + task.title)
    db.commit()
    return serialize(task)


@app.patch('/api/v1/follow-ups/{task_id}')
def edit_follow_up(task_id: str, payload: FollowUpUpdate, ctx=Depends(writer), db=Depends(get_db)):
    task = scoped(db, FollowUp, task_id, ctx['organization_id'], lock=True)
    # S4 – agents may only update follow-ups for leads assigned to them.
    if ctx['role'] == 'agent':
        lead = scoped(db, Lead, task.lead_id, ctx['organization_id'])
        if lead.assigned_to != ctx['user_id']:
            raise HTTPException(403, 'Access to this follow-up is restricted to its assigned agent.')
    task.status = payload.status
    db.flush()
    sync_next_follow_up(db, ctx['organization_id'], task.lead_id)
    activity(db, ctx['organization_id'], task.lead_id, ctx['user_id'], 'follow_up', 'Follow-up ' + payload.status)
    db.commit()
    return serialize(task)


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
    db.commit()
    return {**serialize(job), 'rows': rows[:100], 'preview_limit': 100}


@app.post('/api/v1/imports/commit')
def commit_import(payload: CommitInput, ctx=Depends(writer), db=Depends(get_db)):
    job = scoped(db, ImportJob, payload.import_job_id, ctx['organization_id'], lock=True)
    commit_job(db, ctx, job)
    audit(db, ctx, 'import.committed', job.id)
    db.commit()
    return serialize(job)


@app.get('/api/v1/imports')
def imports(ctx=Depends(context), db=Depends(get_db)):
    return [serialize(j) for j in db.scalars(select(ImportJob).where(ImportJob.organization_id == ctx['organization_id']).order_by(ImportJob.created_at.desc()).limit(100))]


@app.get('/api/v1/imports/{job_id}/errors')
def import_errors(job_id: str, ctx=Depends(context), db=Depends(get_db)):
    scoped(db, ImportJob, job_id, ctx['organization_id'])
    errors = db.scalars(select(ImportErrorRow).where(ImportErrorRow.organization_id == ctx['organization_id'], ImportErrorRow.import_job_id == job_id).order_by(ImportErrorRow.row_number))
    output = io.StringIO()
    csv_writer = csv.writer(output)
    csv_writer.writerow(['row_number', 'code', 'message'])
    for error in errors:
        csv_writer.writerow([error.row_number, safe_csv_cell(error.error_code), safe_csv_cell(error.message)])
    return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="import-errors.csv"'})


@app.get('/api/v1/dashboard/summary')
def dashboard(website_id: str = '', source: str = '', assigned_to: str = '', date_from: datetime | None = None, date_to: datetime | None = None, ctx=Depends(context), db=Depends(get_db)):
    # S4 – agents see only their assigned leads in dashboard stats too.
    eff_assigned_to = agent_scope(ctx) or assigned_to
    query = lead_query(ctx, website_id=website_id, source=source, assigned_to=eff_assigned_to, date_from=date_from, date_to=date_to)
    rows = db.scalars(query).all()
    status_counts = {s: sum(l.status == s for l in rows) for s in ['new', 'contacted', 'qualified', 'converted', 'lost']}
    # Due today is defined in the business timezone, Asia/Kolkata.
    from zoneinfo import ZoneInfo
    local_now = now().astimezone(ZoneInfo('Asia/Kolkata'))
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    task_query = select(FollowUp).join(Lead, (Lead.id == FollowUp.lead_id) & (Lead.organization_id == FollowUp.organization_id)).where(FollowUp.organization_id == ctx['organization_id'], FollowUp.status == 'open', Lead.deleted_at.is_(None), Lead.id.in_(query.with_only_columns(Lead.id)))
    tasks = db.scalars(task_query).all()
    dates = [t.due_at.replace(tzinfo=timezone.utc) for t in tasks]
    sources = {s: sum(l.source == s for l in rows) for s in ['manual', 'website', 'csv', 'xlsx']}
    sites = db.scalars(select(Website).where(Website.organization_id == ctx['organization_id'])).all()
    unaccepted_query = select(Lead).where(Lead.organization_id == ctx['organization_id'], Lead.deleted_at.is_(None), Lead.accepted_by.is_(None))
    unaccepted = db.scalars(unaccepted_query).all()
    unaccepted_leads = [{'id': l.id, 'full_name': l.full_name, 'company_name': l.company_name, 'source': l.source, 'created_at': l.created_at.isoformat() if l.created_at else None} for l in unaccepted[:10]]
    return {'total': len(rows), 'new_in_period': len(rows), 'uncontacted': status_counts['new'], 'unaccepted_count': len(unaccepted), 'unaccepted_leads': unaccepted_leads, 'statuses': status_counts, 'conversion_rate': round(status_counts['converted'] / len(rows) * 100, 1) if rows else 0, 'due_today': sum(start <= d < end for d in dates), 'overdue': sum(d < now() for d in dates), 'sources': sources, 'websites': [{'id': w.id, 'name': w.name, 'count': sum(l.website_id == w.id for l in rows)} for w in sites], 'cohort': 'Leads created within the selected date range', 'timezone': 'Asia/Kolkata'}


@app.get('/api/v1/audit-logs')
def audit_logs(ctx=Depends(admin), db=Depends(get_db)):
    return [serialize(a) for a in db.scalars(select(AuditLog).where(AuditLog.organization_id == ctx['organization_id']).order_by(AuditLog.created_at.desc()).limit(100))]
