"""One-step server database initializer for Timely CRM backend.
Runs migrations and creates the initial organization, admin user, and website API key.
"""
import sys
import subprocess
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Organization, User, Membership, Website, WebsiteKey
from app.security import hash_password, digest

import os
import secrets

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@timelyhealth.in")
ADMIN_PASS  = os.getenv("ADMIN_PASS") or secrets.token_urlsafe(16)
WEBSITE_KEY = os.getenv("WEBSITE_KEY") or f"tly_{secrets.token_urlsafe(32)}"
DOMAIN      = os.getenv("DOMAIN", "digital.timelyhealth.in")

def run_migrations():
    print("[1/3] Running database migrations (alembic upgrade head)...")
    res = subprocess.run(["alembic", "upgrade", "head"], capture_output=True, text=True)
    if res.returncode != 0:
        print("Alembic error:", res.stderr)
        # fallback to sys.executable -m alembic
        res2 = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], capture_output=True, text=True)
        if res2.returncode != 0:
            print("Failed to run migrations:", res2.stderr)
            sys.exit(1)
    print("  -> Database tables created successfully.")

def seed_data():
    print("[2/3] Setting up Organization, Admin User, and Website...")
    with SessionLocal() as db:
        # Organization
        org = db.scalar(select(Organization).where(Organization.slug == "timely-health"))
        if not org:
            org = Organization(name="Timely Health", slug="timely-health", status="active")
            db.add(org)
            db.flush()
            print(f"  -> Created Organization: {org.name} ({org.slug})")
        else:
            print(f"  -> Organization exists: {org.name}")

        # Admin user
        user = db.scalar(select(User).where(User.email == ADMIN_EMAIL.lower()))
        if not user:
            user = User(
                name="Administrator",
                email=ADMIN_EMAIL.lower(),
                password_hash=hash_password(ADMIN_PASS),
                is_active=True
            )
            db.add(user)
            db.flush()
            print(f"  -> Created Admin user: {ADMIN_EMAIL}")
        else:
            print(f"  -> Admin user exists: {ADMIN_EMAIL}")

        # Membership
        mem = db.get(Membership, (org.id, user.id))
        if not mem:
            db.add(Membership(organization_id=org.id, user_id=user.id, role="admin", status="active"))
            db.flush()

        # Website
        website = db.scalar(select(Website).where(Website.organization_id == org.id, Website.domain == DOMAIN))
        if not website:
            website = Website(
                organization_id=org.id,
                name="Timely Health Digital",
                domain=DOMAIN,
                status="active",
                config={}
            )
            db.add(website)
            db.flush()
            print(f"  -> Registered Website: {DOMAIN}")
        else:
            print(f"  -> Website exists: {DOMAIN}")

        # Website Key
        key_hash = digest(WEBSITE_KEY)
        key = db.scalar(select(WebsiteKey).where(WebsiteKey.website_id == website.id, WebsiteKey.key_hash == key_hash))
        if not key:
            db.add(WebsiteKey(
                organization_id=org.id,
                website_id=website.id,
                key_hash=key_hash
            ))
            db.flush()
            print("  -> Website API Key registered.")
        else:
            print("  -> Website API Key is already active.")

        db.commit()

    print("[3/3] Database initialization complete!")
    print("\n-----------------------------------------------------------")
    print("Admin Email    : " + ADMIN_EMAIL)
    print("Admin Password : " + ADMIN_PASS)
    print("Org Slug       : timely-health")
    print("Website Key    : " + WEBSITE_KEY)
    print("Ingest URL     : https://crmbackend.iryax.com/api/v1/public/leads")
    print("-----------------------------------------------------------\n")

if __name__ == "__main__":
    run_migrations()
    seed_data()
