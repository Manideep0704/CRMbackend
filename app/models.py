from .db import now, uid


class ModelDict(dict):
    """
    Dictionary wrapper that allows dot notation attribute access
    while maintaining 100% dictionary compatibility with PyMongo and FastAPI.
    """
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            return None

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            pass


def Organization(name: str, slug: str, status: str = 'active', id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'name': name,
        'slug': slug,
        'status': status,
        'created_at': now()
    })


def User(
    email: str,
    name: str = '',
    password_hash: str = '',
    is_active: bool = True,
    auth_provider: str = 'local',
    auth_subject: str = None,
    token_version: int = 1,
    id: str = None
) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'name': name,
        'email': email.lower().strip(),
        'password_hash': password_hash,
        'is_active': is_active,
        'auth_provider': auth_provider,
        'auth_subject': auth_subject,
        'token_version': token_version,
        'sessions_revoked_at': None
    })


def Membership(organization_id: str, user_id: str, role: str = 'agent', status: str = 'active') -> ModelDict:
    mem_id = f"{organization_id}:{user_id}"
    return ModelDict({
        '_id': mem_id,
        'id': mem_id,
        'organization_id': organization_id,
        'user_id': user_id,
        'role': role,
        'status': status
    })


def Website(
    organization_id: str,
    name: str,
    domain: str,
    status: str = 'active',
    config: dict = None,
    id: str = None
) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'name': name,
        'domain': domain.lower().strip(),
        'status': status,
        'config': config or {},
        'created_at': now()
    })


def WebsiteKey(organization_id: str, website_id: str, key_hash: str, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'website_id': website_id,
        'key_hash': key_hash,
        'created_at': now()
    })


def Lead(
    organization_id: str,
    full_name: str,
    phone: str = '',
    email: str = '',
    company_name: str = '',
    inquiry_type: str = '',
    status: str = 'new',
    priority: str = 'medium',
    website_id: str = None,
    assigned_to: str = None,
    accepted_by: str = None,
    accepted_at = None,
    source: str = 'manual',
    duplicate_of: str = None,
    external_submission_id: str = None,
    attribution: dict = None,
    tags: list = None,
    next_follow_up_at = None,
    id: str = None,
    **extra
) -> ModelDict:
    item_id = id or uid()
    lead_dict = {
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'website_id': website_id,
        'full_name': full_name,
        'phone': phone,
        'email': email.lower().strip() if email else '',
        'company_name': company_name,
        'inquiry_type': inquiry_type,
        'status': status,
        'priority': priority,
        'assigned_to': assigned_to,
        'accepted_by': accepted_by,
        'accepted_at': accepted_at,
        'source': source,
        'duplicate_of': duplicate_of,
        'external_submission_id': external_submission_id,
        'attribution': attribution or {},
        'tags': tags or [],
        'next_follow_up_at': next_follow_up_at,
        'deleted_at': None,
        'created_at': now(),
        'updated_at': now()
    }
    lead_dict.update(extra)
    return ModelDict(lead_dict)


def Activity(organization_id: str, lead_id: str, user_id: str, type: str, description: str, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'lead_id': lead_id,
        'user_id': user_id,
        'type': type,
        'description': description,
        'created_at': now()
    })


def Note(organization_id: str, lead_id: str, user_id: str, description: str, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'lead_id': lead_id,
        'user_id': user_id,
        'description': description,
        'created_at': now()
    })


def FollowUp(
    organization_id: str,
    lead_id: str,
    due_at,
    title: str = 'Scheduled follow-up',
    assigned_to: str = None,
    status: str = 'open',
    id: str = None
) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'lead_id': lead_id,
        'assigned_to': assigned_to,
        'due_at': due_at,
        'status': status,
        'title': title,
        'created_at': now()
    })


def AuthSession(token_hash: str, user_id: str, organization_id: str, expires_at, token_version: int = 1) -> ModelDict:
    return ModelDict({
        '_id': token_hash,
        'token_hash': token_hash,
        'user_id': user_id,
        'organization_id': organization_id,
        'expires_at': expires_at,
        'created_at': now(),
        'token_version': token_version
    })


def ImportJob(organization_id: str, uploaded_by: str, filename: str, rows, counts: dict, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'uploaded_by': uploaded_by,
        'filename': filename,
        'status': 'pending',
        'rows': rows,
        'counts': counts,
        'created_at': now()
    })


def ImportErrorRow(organization_id: str, import_job_id: str, row_number: int, error_code: str, message: str, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'import_job_id': import_job_id,
        'row_number': row_number,
        'error_code': error_code,
        'message': message
    })


def AuditLog(organization_id: str, actor_id: str, action: str, target: str, id: str = None) -> ModelDict:
    item_id = id or uid()
    return ModelDict({
        '_id': item_id,
        'id': item_id,
        'organization_id': organization_id,
        'actor_id': actor_id,
        'action': action,
        'target': target,
        'created_at': now()
    })
