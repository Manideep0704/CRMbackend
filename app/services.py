from datetime import timezone
from fastapi import HTTPException
from sqlalchemy import or_, select
from .db import now
from .models import Activity, AuditLog, FollowUp, Lead, Membership, Website


def scoped(db, model, item_id, organization_id, lock=False):
    query = select(model).where(model.id == item_id, model.organization_id == organization_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if not item or (isinstance(item, Lead) and item.deleted_at):
        raise HTTPException(404, 'Record not found')
    return item


def check_assignee(db, org, user_id):
    if user_id:
        member = db.get(Membership, (org, user_id))
        if not member or member.status != 'active' or member.role == 'viewer':
            raise HTTPException(422, 'Choose an active agent or administrator in this organization')


def duplicate(db, org, data, exclude=None):
    terms = []
    if data.email:
        terms.append(Lead.email == data.email)
    if data.phone:
        terms.append(Lead.phone == data.phone)
    if not terms:
        return None
    query = select(Lead).where(Lead.organization_id == org, Lead.deleted_at.is_(None), or_(*terms))
    if exclude:
        query = query.where(Lead.id != exclude)
    return db.scalar(query.order_by(Lead.created_at, Lead.id))


def activity(db, org, lead_id, user_id, kind, description):
    db.add(Activity(organization_id=org, lead_id=lead_id, user_id=user_id, type=kind, description=description))


def audit(db, ctx, action, target):
    db.add(AuditLog(organization_id=ctx['organization_id'], actor_id=ctx.get('user_id'), action=action, target=target))


def create_lead(db, org, data, source, actor=None, external_id=None, attribution=None):
    values = data.model_dump()
    if data.website_id:
        scoped(db, Website, data.website_id, org)
    check_assignee(db, org, data.assigned_to)
    if external_id and data.website_id:
        prior = db.scalar(select(Lead).where(Lead.website_id == data.website_id, Lead.external_submission_id == external_id, Lead.organization_id == org))
        if prior:
            return prior, True
    match = duplicate(db, org, data)
    lead = Lead(**values, organization_id=org, source=source, duplicate_of=match.id if match else None, external_submission_id=external_id, attribution=attribution or {})
    db.add(lead)
    db.flush()
    activity(db, org, lead.id, actor, 'created', f'Lead created via {source}' + ('; possible duplicate flagged' if match else ''))
    if data.next_follow_up_at:
        db.add(FollowUp(organization_id=org, lead_id=lead.id, assigned_to=data.assigned_to, due_at=data.next_follow_up_at, title='Scheduled follow-up'))
    return lead, False


def serialize(item):
    result = {}
    for column in item.__table__.columns:
        key = column.name
        if key in {'password_hash', 'key_hash', 'token_hash', 'rows'}:
            continue
        value = getattr(item, key)
        if hasattr(value, 'isoformat'):
            value = value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()
        result[key] = value
    return result


def sync_next_follow_up(db, org, lead_id):
    lead = scoped(db, Lead, lead_id, org)
    dates = db.scalars(select(FollowUp.due_at).where(FollowUp.organization_id == org, FollowUp.lead_id == lead_id, FollowUp.status == 'open')).all()
    lead.next_follow_up_at = min(dates) if dates else None
    lead.updated_at = now()
