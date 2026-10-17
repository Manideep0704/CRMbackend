import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from .db import get_db, now
from .models import AuthSession, Membership, Organization, User


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
    db.add(AuthSession(
        token_hash=digest(token),
        user_id=user.id,
        organization_id=organization_id,
        expires_at=now() + timedelta(hours=12),
        created_at=now(),
        token_version=getattr(user, 'token_version', 1) or 1
    ))
    return token


def revoke_user_sessions(db, user_id: str):
    """
    S5 – Permanently revokes all active sessions for a user.
    Deletes session records and increments user token_version so old tokens
    cannot be revived even if the account is later re-enabled.
    """
    from sqlalchemy import delete
    user = db.get(User, user_id)
    if user:
        user.token_version = (getattr(user, 'token_version', 1) or 1) + 1
        user.sessions_revoked_at = now()
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    db.flush()


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
    # Always trust localhost and private LAN origins in non-production only.
    if not is_production:
        trusted.update({
            'http://localhost:3000',
            'http://127.0.0.1:3000',
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
    session = db.get(AuthSession, digest(token))
    if not session or session.expires_at.replace(tzinfo=timezone.utc) <= now():
        raise HTTPException(401, 'Session expired')
    user = db.get(User, session.user_id)
    member = db.get(Membership, (session.organization_id, session.user_id))
    org = db.get(Organization, session.organization_id)
    if not user or not user.is_active or not member or member.status != 'active' or not org or org.status != 'active':
        raise HTTPException(403, 'Organization access is inactive')
    # S5 – Session revocation and token versioning checks
    sess_version = getattr(session, 'token_version', 1) or 1
    user_version = getattr(user, 'token_version', 1) or 1
    if sess_version != user_version:
        raise HTTPException(401, 'Session has been revoked')
    revoked_at = getattr(user, 'sessions_revoked_at', None)
    sess_created = getattr(session, 'created_at', None)
    if revoked_at and sess_created:
        rev_dt = revoked_at if revoked_at.tzinfo else revoked_at.replace(tzinfo=timezone.utc)
        crt_dt = sess_created if sess_created.tzinfo else sess_created.replace(tzinfo=timezone.utc)
        if crt_dt <= rev_dt:
            raise HTTPException(401, 'Session has been revoked')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('X-Timely-Request') != '1':
            raise HTTPException(403, 'Missing request protection header')
        origin = request.headers.get('origin')
        if not is_trusted_origin(origin):
            raise HTTPException(403, 'Untrusted origin')
    return {'organization_id': org.id, 'user_id': user.id, 'role': member.role, 'user': user, 'organization': org}


def writer(ctx=Depends(context)):
    if ctx['role'] not in ('admin', 'agent'):
        raise HTTPException(403, 'Editing requires an agent or administrator role')
    return ctx


def admin(ctx=Depends(context)):
    if ctx['role'] != 'admin':
        raise HTTPException(403, 'Administrator role required')
    return ctx
