from datetime import timezone
from fastapi import HTTPException
from .db import now
from .models import Activity, AuditLog, FollowUp, Lead, ModelDict


def scoped(db, collection, item_id, organization_id, lock=False):
    col_name = collection if isinstance(collection, str) else getattr(collection, '__tablename__', str(collection).lower())
    # Handle both string collection name and class reference
    if not isinstance(collection, str) and hasattr(collection, '__name__'):
        col_name = collection.__name__.lower() + 's'
        if col_name == 'activitys': col_name = 'activities'

    item = db[col_name].find_one({
        '$and': [
            {'$or': [{'_id': item_id}, {'id': item_id}]},
            {'organization_id': organization_id}
        ]
    })
    if not item or (col_name == 'leads' and item.get('deleted_at')):
        raise HTTPException(404, 'Record not found')
    return ModelDict(item)


def check_assignee(db, org, user_id):
    if user_id:
        mem_id = f"{org}:{user_id}"
        member = db['memberships'].find_one({
            '$or': [
                {'_id': mem_id},
                {'organization_id': org, 'user_id': user_id}
            ]
        })
        if not member:
            user = db['users'].find_one({'$or': [{'_id': user_id}, {'id': user_id}]})
            if user:
                db['memberships'].update_one(
                    {'_id': mem_id},
                    {'$set': {'_id': mem_id, 'id': mem_id, 'organization_id': org, 'user_id': user_id, 'role': 'agent', 'status': 'active'}},
                    upsert=True
                )
                return
            raise HTTPException(422, 'Choose an active agent or administrator in this organization')
        if member.get('status') != 'active':
            db['memberships'].update_one({'_id': member['_id']}, {'$set': {'status': 'active'}})
        if member.get('role') == 'viewer':
            db['memberships'].update_one({'_id': member['_id']}, {'$set': {'role': 'agent'}})


def duplicate(db, org, data, exclude=None):
    terms = []
    if getattr(data, 'email', None):
        terms.append({'email': data.email.lower().strip()})
    if getattr(data, 'phone', None):
        terms.append({'phone': data.phone.strip()})
    if not terms:
        return None

    query = {
        'organization_id': org,
        'deleted_at': None,
        '$or': terms
    }
    if exclude:
        query['$and'] = [
            {'_id': {'$ne': exclude}},
            {'id': {'$ne': exclude}}
        ]

    item = db['leads'].find_one(query, sort=[('created_at', 1), ('_id', 1)])
    return ModelDict(item) if item else None


def activity(db, org, lead_id, user_id, kind, description):
    doc = Activity(organization_id=org, lead_id=lead_id, user_id=user_id, type=kind, description=description)
    db['activities'].insert_one(doc)


def audit(db, ctx, action, target):
    doc = AuditLog(organization_id=ctx['organization_id'], actor_id=ctx.get('user_id'), action=action, target=target)
    db['audit_logs'].insert_one(doc)


def create_lead(db, org, data, source, actor=None, external_id=None, attribution=None):
    values = data.model_dump() if hasattr(data, 'model_dump') else dict(data)
    if values.get('website_id'):
        site = db['websites'].find_one({
            '$and': [
                {'$or': [{'_id': values['website_id']}, {'id': values['website_id']}]},
                {'organization_id': org}
            ]
        })
        if site:
            site_config = site.get('config') or {}
            if not values.get('assigned_to') and site_config:
                default_assignee = site_config.get('assigned_to') or site_config.get('default_assignee')
                if default_assignee:
                    check_assignee(db, org, default_assignee)
                    values['assigned_to'] = default_assignee

    check_assignee(db, org, values.get('assigned_to'))

    if external_id and values.get('website_id'):
        prior = db['leads'].find_one({
            'website_id': values['website_id'],
            'external_submission_id': external_id,
            'organization_id': org
        })
        if prior:
            return ModelDict(prior), True

    match = duplicate(db, org, data)
    assigned = values.get('assigned_to')
    if actor and assigned and assigned == actor:
        values['accepted_by'] = actor
        values['accepted_at'] = now()
    else:
        values['accepted_by'] = None
        values['accepted_at'] = None

    lead_doc = Lead(
        organization_id=org,
        source=source,
        duplicate_of=match.id if match else None,
        external_submission_id=external_id,
        attribution=attribution or {},
        **values
    )
    db['leads'].insert_one(lead_doc)

    activity(db, org, lead_doc.id, actor, 'created', f'Lead created via {source}' + ('; possible duplicate flagged' if match else ''))

    if getattr(data, 'next_follow_up_at', None):
        db['follow_ups'].insert_one(FollowUp(
            organization_id=org,
            lead_id=lead_doc.id,
            assigned_to=values.get('assigned_to'),
            due_at=data.next_follow_up_at,
            title='Scheduled follow-up'
        ))

    return lead_doc, False


def serialize(item):
    if item is None:
        return None
    d = dict(item)
    # Standardize id
    if '_id' in d and 'id' not in d:
        d['id'] = str(d['_id'])
    elif 'id' in d:
        d['_id'] = str(d.get('_id', d['id']))

    # Strip sensitive hashes
    for key in ['password_hash', 'key_hash', 'token_hash']:
        d.pop(key, None)

    # Format datetimes
    for k, v in list(d.items()):
        if hasattr(v, 'isoformat'):
            d[k] = v.replace(tzinfo=timezone.utc).isoformat() if v.tzinfo is None else v.isoformat()
    return d


def sync_next_follow_up(db, org, lead_id):
    lead = scoped(db, 'leads', lead_id, org)
    cursor = db['follow_ups'].find({
        'organization_id': org,
        'lead_id': lead_id,
        'status': 'open'
    }).sort('due_at', 1)
    docs = list(cursor)
    next_at = docs[0]['due_at'] if docs else None
    db['leads'].update_one(
        {'$or': [{'_id': lead_id}, {'id': lead_id}]},
        {'$set': {'next_follow_up_at': next_at, 'updated_at': now()}}
    )
