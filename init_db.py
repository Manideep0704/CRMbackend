"""One-step server database initializer for Timely CRM backend with MongoDB Atlas.
Ensures MongoDB indexes and creates the initial organization, admin user, and website API key.
"""
import os
import secrets
from app.db import get_crm_db, init_mongo_indexes
from app.models import Membership, Organization, User, Website, WebsiteKey
from app.security import digest, hash_password

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@timelyhealth.in")
ADMIN_PASS  = os.getenv("ADMIN_PASS") or secrets.token_urlsafe(16)
WEBSITE_KEY = os.getenv("WEBSITE_KEY") or f"tly_{secrets.token_urlsafe(32)}"
DOMAIN      = os.getenv("DOMAIN", "digital.timelyhealth.in")


def init_database():
    print("[1/3] Ensuring MongoDB indexes...")
    init_mongo_indexes()
    db = get_crm_db()

    print("[2/3] Setting up Organization, Admin User, and Website in MongoDB crmDB...")

    # 1. Organization
    org = db['organizations'].find_one({'slug': "timely-health"})
    if not org:
        org = Organization(name="Timely Health", slug="timely-health", status="active")
        db['organizations'].insert_one(org)
        print(f"  -> Created Organization: {org['name']} ({org['slug']})")
    else:
        print(f"  -> Organization exists: {org['name']}")

    org_id = org.get('id') or str(org.get('_id'))

    # 2. Admin User
    email_clean = ADMIN_EMAIL.lower().strip()
    user = db['users'].find_one({'email': email_clean})
    if not user:
        user = User(
            name="Administrator",
            email=email_clean,
            password_hash=hash_password(ADMIN_PASS),
            is_active=True,
            auth_provider='local',
            token_version=1
        )
        db['users'].insert_one(user)
        print(f"  -> Created Admin user: {ADMIN_EMAIL}")
    else:
        print(f"  -> Admin user exists: {ADMIN_EMAIL}")

    user_id = user.get('id') or str(user.get('_id'))

    # 3. Membership
    mem_id = f"{org_id}:{user_id}"
    mem = db['memberships'].find_one({'$or': [{'_id': mem_id}, {'organization_id': org_id, 'user_id': user_id}]})
    if not mem:
        db['memberships'].insert_one(Membership(organization_id=org_id, user_id=user_id, role="admin", status="active"))
        print("  -> Assigned Admin membership role.")
    else:
        print("  -> Admin membership is active.")

    # 4. Website
    website = db['websites'].find_one({'organization_id': org_id, 'domain': DOMAIN})
    if not website:
        website = Website(
            organization_id=org_id,
            name="Timely Health Digital",
            domain=DOMAIN,
            status="active",
            config={}
        )
        db['websites'].insert_one(website)
        print(f"  -> Registered Website: {DOMAIN}")
    else:
        print(f"  -> Website exists: {DOMAIN}")

    website_id = website.get('id') or str(website.get('_id'))

    # 5. Website Key
    key_hash = digest(WEBSITE_KEY)
    key = db['website_keys'].find_one({'website_id': website_id, 'key_hash': key_hash})
    if not key:
        db['website_keys'].insert_one(WebsiteKey(
            organization_id=org_id,
            website_id=website_id,
            key_hash=key_hash
        ))
        print("  -> Website API Key registered.")
    else:
        print("  -> Website API Key is already active.")

    print("[3/3] MongoDB Database initialization complete!")
    print("\n-----------------------------------------------------------")
    print("Database       : MongoDB Atlas (crmDB)")
    print("Admin Email    : " + ADMIN_EMAIL)
    print("Admin Password : " + ADMIN_PASS)
    print("Org Slug       : timely-health")
    print("Website Key    : " + WEBSITE_KEY)
    print("Ingest URL     : https://crmbackend.iryax.com/api/v1/public/leads")
    print("-----------------------------------------------------------\n")


if __name__ == '__main__':
    init_database()
