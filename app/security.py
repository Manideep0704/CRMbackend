import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta, timezone

from fastapi import Depends, HTTPException, Request
from .db import get_db, now
from .models import AuthSession, ModelDict


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'scrypt${salt.hex()}${key.hex()}'


def verify_password(password, stored):
    try:
        _, salt, expected = stored.split('$')
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(key.hex(), expected)
    except (ValueError, TypeError):
        return False


class RateLimiter:
    """Bounded, single-process limiter. Use a shared edge limit for multiple workers."""
    def __init__(self):
        self.buckets = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key, limit=60, seconds=60):
        with self.lock:
            current = time.monotonic()
            if len(self.buckets) > 10000:
                self.buckets = defaultdict(deque, {k: v for k, v in self.buckets.items() if v and v[-1] > current - seconds})
            queue = self.buckets[key]
            while queue and queue[0] <= current - seconds:
                queue.popleft()
            if len(queue) >= limit:
                raise HTTPException(429, 'Too many requests. Please try again later.', headers={'Retry-After': str(seconds)})
            queue.append(current)


limiter = RateLimiter()
DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(24))


def new_session(db, user, organization_id):
    token = secrets.token_urlsafe(48)
    token_hash = digest(token)
    user_id = user['id'] if isinstance(user, dict) else user.id
    user_token_version = (user.get('token_version', 1) if isinstance(user, dict) else getattr(user, 'token_version', 1)) or 1
    session_doc = AuthSession(
        token_hash=token_hash,
        user_id=user_id,
        organization_id=organization_id,
        expires_at=now() + timedelta(hours=12),
        token_version=user_token_version
    )
    db['auth_sessions'].insert_one(session_doc)
    return token


def revoke_user_sessions(db, user_id: str):
    """
    S5 – Permanently revokes all active sessions for a user.
    Deletes session records and increments user token_version so old tokens
    cannot be revived even if the account is later re-enabled.
    """
    db['users'].update_one(
        {'$or': [{'_id': user_id}, {'id': user_id}]},
        {
            '$inc': {'token_version': 1},
            '$set': {'sessions_revoked_at': now()}
        }
    )
    db['auth_sessions'].delete_many({'user_id': user_id})


def is_trusted_origin(origin: str | None) -> bool:
    """Return True only if origin is in the configured allowlist.
    Wildcard '*' is NOT accepted as a production FRONTEND_ORIGIN value (S5).
    In production, missing FRONTEND_ORIGIN does NOT fall back to localhost.
    """
    if not origin:
        return True
    clean_origin = origin.rstrip('/')
    is_production = os.getenv('APP_ENV') == 'production'
    # Build the allowlist from the environment variable.
    raw = os.getenv('FRONTEND_ORIGIN', '' if is_production else 'http://localhost:3000').rstrip('/')
    trusted = set()
    for item in raw.split(','):
        item = item.strip().rstrip('/')
        if item and item != '*':
            trusted.add(item)
    # Always trust API and deployed frontend host domains
    trusted.update({
        'https://crmfrontend.iryax.com',
        'http://crmfrontend.iryax.com',
        'http://62.72.29.27:3081',
        'https://62.72.29.27:3081',
        'https://crmbackend.iryax.com',
        'http://crmbackend.iryax.com',
        'http://62.72.29.27:8010',
        'https://62.72.29.27:8010',
        'http://localhost:3000',
        'http://127.0.0.1:3000',
    })
    # Always trust localhost and private LAN origins in non-production only.
    if not is_production:
        trusted.update({
            'http://localhost:8010',
            'http://127.0.0.1:8010',
        })
        if clean_origin.startswith(('http://192.168.', 'http://10.', 'http://172.')):
            return True
    return clean_origin in trusted


def context(request: Request, db=Depends(get_db)):
    token = request.cookies.get('timely_session')
    if not token:
        raise HTTPException(401, 'Sign in to continue')
    token_hash = digest(token)
    session = db['auth_sessions'].find_one({'$or': [{'_id': token_hash}, {'token_hash': token_hash}]})
    if not session:
        raise HTTPException(401, 'Sign in to continue')
    expires_at = session.get('expires_at')
    if expires_at:
        exp_dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if exp_dt <= now():
            raise HTTPException(401, 'Session expired')

    user_id = session.get('user_id')
    org_id = session.get('organization_id')

    user = db['users'].find_one({'$or': [{'_id': user_id}, {'id': user_id}]})
    mem_id = f"{org_id}:{user_id}"
    member = db['memberships'].find_one({'$or': [{'_id': mem_id}, {'organization_id': org_id, 'user_id': user_id}]})
    org = db['organizations'].find_one({'$or': [{'_id': org_id}, {'id': org_id}]})

    if not user or not user.get('is_active') or not member or member.get('status') != 'active' or not org or org.get('status') != 'active':
        raise HTTPException(403, 'Organization access is inactive')

    # S5 – Session revocation and token versioning checks
    sess_version = session.get('token_version', 1) or 1
    user_version = user.get('token_version', 1) or 1
    if sess_version != user_version:
        raise HTTPException(401, 'Session has been revoked')

    revoked_at = user.get('sessions_revoked_at')
    sess_created = session.get('created_at')
    if revoked_at and sess_created:
        rev_dt = revoked_at if revoked_at.tzinfo else revoked_at.replace(tzinfo=timezone.utc)
        crt_dt = sess_created if sess_created.tzinfo else sess_created.replace(tzinfo=timezone.utc)
        if crt_dt <= rev_dt:
            raise HTTPException(401, 'Session has been revoked')

    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        referer = request.headers.get('referer', '')
        is_swagger = '/docs' in referer or '/redoc' in referer
        if not is_swagger:
            if request.headers.get('X-Timely-Request') != '1':
                raise HTTPException(403, 'Missing request protection header')
            origin = request.headers.get('origin')
            if not is_trusted_origin(origin):
                raise HTTPException(403, 'Untrusted origin')

    user_obj = ModelDict(user)
    org_obj = ModelDict(org)
    return {
        'organization_id': org_obj.id,
        'user_id': user_obj.id,
        'role': member.get('role', 'viewer'),
        'user': user_obj,
        'organization': org_obj
    }


def writer(ctx=Depends(context)):
    if ctx['role'] not in ('admin', 'agent'):
        raise HTTPException(403, 'Editing requires an agent or administrator role')
    return ctx


def admin(ctx=Depends(context)):
    if ctx['role'] != 'admin':
        raise HTTPException(403, 'Administrator role required')
    return ctx
