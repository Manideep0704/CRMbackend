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
    db.add(AuthSession(token_hash=digest(token), user_id=user.id, organization_id=organization_id, expires_at=now() + timedelta(hours=12)))
    return token


def is_trusted_origin(origin: str | None) -> bool:
    if not origin:
        return True
    clean_origin = origin.rstrip('/')
    allowed = os.getenv('FRONTEND_ORIGIN', 'http://localhost:3000').rstrip('/')
    if allowed == '*':
        return True
    trusted = {
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        'http://localhost:8010',
        'http://127.0.0.1:8010',
    }
    for item in allowed.split(','):
        if item.strip():
            trusted.add(item.strip().rstrip('/'))
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
