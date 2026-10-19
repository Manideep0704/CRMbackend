"""Explicit, idempotent initial account creation. No default production password."""
import argparse
import getpass
import os


def seed(email, password, domain=None):
    if len(password) < 12:
        raise ValueError('Use a password with at least 12 characters')
    from .db import get_crm_db
    from .models import Membership, Organization, User, Website
    from .security import hash_password
    db = get_crm_db()
    email_clean = email.strip().lower()
    org = db.organizations.find_one({'slug': 'timely-health'})
    if not org:
        org = Organization(name='Timely Health', slug='timely-health')
        db.organizations.insert_one(dict(org))
    user = db.users.find_one({'email': email_clean})
    if not user:
        user = User(name='Timely Administrator', email=email_clean, password_hash=hash_password(password))
        db.users.insert_one(dict(user))
    org_id = str(org.get('id') or org.get('_id'))
    user_id = str(user.get('id') or user.get('_id'))
    if not db.memberships.find_one({'organization_id': org_id, 'user_id': user_id}):
        db.memberships.insert_one(dict(Membership(organization_id=org_id, user_id=user_id, role='admin')))
    if domain and not db.websites.find_one({'organization_id': org_id, 'domain': domain}):
        db.websites.insert_one(dict(Website(organization_id=org_id, name='Timely Health Digital', domain=domain, config={})))
    print('Timely Health organization and administrator are ready in MongoDB. Existing passwords were not changed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--email', required=True)
    parser.add_argument('--domain', help='Verified domain only; omit until confirmed')
    args = parser.parse_args()
    seed(args.email, os.getenv('SEED_PASSWORD') or getpass.getpass('Administrator password: '), args.domain)
