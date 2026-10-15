"""Explicit, idempotent initial account creation. No default production password."""
import argparse
import getpass
import os
from sqlalchemy import select
from .db import SessionLocal
from .models import Membership, Organization, User, Website
from .security import hash_password


def seed(email, password, domain=None):
    if len(password) < 12:
        raise ValueError('Use a password with at least 12 characters')
    with SessionLocal() as db:
        org = db.scalar(select(Organization).where(Organization.slug == 'timely-health'))
        if not org:
            org = Organization(name='Timely Health', slug='timely-health')
            db.add(org)
            db.flush()
        user = db.scalar(select(User).where(User.email == email.lower()))
        if not user:
            user = User(name='Timely Administrator', email=email.lower(), password_hash=hash_password(password))
            db.add(user)
            db.flush()
        if not db.get(Membership, (org.id, user.id)):
            db.add(Membership(organization_id=org.id, user_id=user.id, role='admin'))
        if domain and not db.scalar(select(Website).where(Website.organization_id == org.id, Website.domain == domain)):
            db.add(Website(organization_id=org.id, name='Timely Health Digital', domain=domain, config={}))
        db.commit()
    print('Timely Health organization and administrator are ready. Existing passwords were not changed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--email', required=True)
    parser.add_argument('--domain', help='Verified domain only; omit until confirmed')
    args = parser.parse_args()
    seed(args.email, os.getenv('SEED_PASSWORD') or getpass.getpass('Administrator password: '), args.domain)
