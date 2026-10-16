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
from .security import DUMMY_PASSWORD, admin, context, digest, hash_password, is_trusted_origin, limiter, new_session, verify_password, writer
from .services import activity, audit, check_assignee, create_lead, scoped, serialize, sync_next_follow_up

app = FastAPI(title='Timely CRM', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:3000', 'http://127.0.0.1:3000', 'http://localhost:8010', 'http://127.0.0.1:8010'], allow_origin_regex=r'https?://.*', allow_credentials=True, allow_methods=['*'], allow_headers=['*'])


@app.on_event("startup")
def startup_db_init():
    from .db import Base, engine, SessionLocal
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        for col, col_type in [('accepted_by', 'VARCHAR(36)'), ('accepted_at', 'DATETIME')]:
            try:
                conn.execute(text(f"ALTER TABLE leads ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass
    with SessionLocal() as db:
        org = db.scalar(select(Organization).where(Organization.slug == "timely-health"))
        if not org:
            org = Organization(name="Timely Health", slug="timely-health", status="active")
            db.add(org)
            db.flush()
        user = db.scalar(select(User).where(User.email == "admin@timelyhealth.in"))
        if not user:
            user = User(
                name="Administrator",
                email="admin@timelyhealth.in",
                password_hash=hash_password("TimelyAdmin2026!"),
                is_active=True
            )
            db.add(user)
            db.flush()
        if not db.get(Membership, (org.id, user.id)):
            db.add(Membership(organization_id=org.id, user_id=user.id, role="admin", status="active"))
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
        key_hash = digest("tly_9Cy-8gtWI2FMMh_GpxLTwfFr5Iav7gY7-cR-1JxnSinXwaFwhc9enQ")
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


@app.get('/api/v1/public/init-db')
def manual_init_db():
    startup_db_init()
    return {'status': 'ok', 'message': 'Database initialized successfully'}


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
    limiter.check(('login', request.client.host), 10, 300)
    origin = request.headers.get('origin')
    if request.headers.get('X-Timely-Request') != '1' or not is_trusted_origin(origin):
        raise HTTPException(403, 'Untrusted sign-in request')
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD)
    org = db.scalar(select(Organization).where(Organization.slug == payload.organization_slug, Organization.status == 'active'))
    if not org:
        org = db.scalar(select(Organization).where(Organization.slug == 'timely-health', Organization.status == 'active'))

    # If local authentication failed, check attendanceDB in MongoDB
    if not valid or not user:
        from .mongo import authenticate_attendance_user
        mongo_user = authenticate_attendance_user(payload.email, payload.password)
        if mongo_user:
            if not org:
                org = Organization(name='Timely Health', slug='timely-health', status='active')
                db.add(org)
                db.flush()

            user = db.scalar(select(User).where(User.email == mongo_user['email']))
            if not user:
                user = User(
                    name=mongo_user['name'],
                    email=mongo_user['email'],
                    password_hash=hash_password(payload.password),
                    is_active=True
                )
                db.add(user)
                db.flush()
            else:
                user.password_hash = hash_password(payload.password)
                if mongo_user.get('name') and not user.name:
                    user.name = mongo_user['name']
                db.flush()

            membership = db.get(Membership, (org.id, user.id))
            if not membership:
                membership = Membership(
                    organization_id=org.id,
                    user_id=user.id,
                    role=mongo_user.get('role', 'agent'),
                    status='active'
                )
                db.add(membership)
                db.flush()
            valid = True

    membership = db.get(Membership, (org.id, user.id)) if org and user else None
    if not valid or not user or not user.is_active or not membership or membership.status != 'active':
        raise HTTPException(401, 'Invalid email, password or organization')
    token = new_session(db, user, org.id)
    db.commit()
    is_secure = os.getenv('APP_ENV') == 'production' and not (origin and (origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1')))
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
def leads(q: str = '', status: str = '', website_id: str = '', source: str = '', assigned_to: str = '', pipeline: bool = False, date_from: datetime | None = None, date_to: datetime | None = None, page: int = 1, page_size: int = 20, sort: str = 'newest', ctx=Depends(context), db=Depends(get_db)):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, 'Invalid pagination')
    
    # In deal pipeline: admin sees all leads (or filters by agent); agent strictly sees only their assigned leads
    eff_assigned_to = assigned_to
    if pipeline and ctx['role'] == 'agent':
        eff_assigned_to = ctx['user_id']

    query = lead_query(ctx, q, status, website_id, source, eff_assigned_to, date_from, date_to)
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    order = {'newest': Lead.created_at.desc(), 'oldest': Lead.created_at.asc(), 'name': Lead.full_name.asc()}.get(sort)
    if order is None:
        raise HTTPException(422, 'Sort must be newest, oldest or name')
    return {'items': [serialize(l) for l in db.scalars(query.order_by(order, Lead.id).offset((page - 1) * page_size).limit(page_size))], 'total': count, 'page': page, 'page_size': page_size}


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

        writer.writerow([
            idx,
            f"TL-{lead.id[:8].upper()}",
            lead.full_name or '',
            clean_phone,
            lead.email or '',
            lead.company_name or '',
            lead.inquiry_type or '',
            (lead.status or '').capitalize(),
            (lead.priority or '').capitalize(),
            formatted_source,
            websites_dict.get(lead.website_id, 'Timely Health Digital'),
            members_dict.get(lead.assigned_to, 'Unassigned'),
            next_follow,
            created_str
        ])

    audit(db, ctx, 'leads.exported', f'count:{len(leads)}')
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
    scoped(db, Lead, lead_id, ctx['organization_id'])
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
    return [{**serialize(task), 'lead_name': name} for task, name in db.execute(query.order_by(FollowUp.due_at))]


@app.post('/api/v1/follow-ups', status_code=201)
def add_follow_up(payload: FollowUpInput, ctx=Depends(writer), db=Depends(get_db)):
    scoped(db, Lead, payload.lead_id, ctx['organization_id'])
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
    query = lead_query(ctx, website_id=website_id, source=source, assigned_to=assigned_to, date_from=date_from, date_to=date_to)
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
