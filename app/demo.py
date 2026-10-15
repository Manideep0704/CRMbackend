"""Create only synthetic development records. Never run against production."""
import os
import secrets
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select
from .db import SessionLocal, now
from .models import Organization, User, Website
from .schemas import LeadInput
from .seed import seed
from .services import create_lead


def main():
    if os.getenv('APP_ENV') == 'production':
        raise RuntimeError('Demo data is disabled in production')
    credentials = Path('.env.demo.local')
    if credentials.exists():
        print('Demo already initialized. Credentials are in backend/.env.demo.local (ignored by Git).')
        return
    password = secrets.token_urlsafe(20)
    seed('demo@timely.local', password, 'digital.timelyhealth.in')
    with SessionLocal() as db:
        org = db.scalar(select(Organization).where(Organization.slug == 'timely-health'))
        user = db.scalar(select(User).where(User.email == 'demo@timely.local'))
        website = db.scalar(select(Website).where(Website.organization_id == org.id))
        names = ['Aarav Shah', 'Meera Iyer', 'Rohan Verma', 'Ananya Rao', 'Kabir Mehta', 'Sara Thomas', 'Vivaan Patel', 'Diya Kapoor', 'Arjun Nair', 'Ishita Sen', 'Aditya Rao', 'Nisha Das']
        states = ['new', 'qualified', 'contacted', 'converted', 'new', 'qualified', 'contacted', 'lost', 'new', 'converted', 'new', 'contacted']
        for i, name in enumerate(names):
            lead, _ = create_lead(db, org.id, LeadInput(full_name=name, email=f'sample{i+1}@example.com', company_name=['Sample Clinic', 'Example Wellness', 'Demo Health Co.'][i % 3], inquiry_type='Digital consultation', status=states[i], priority=['medium', 'high', 'low'][i % 3], website_id=website.id if i % 3 == 0 else None, assigned_to=user.id, next_follow_up_at=(now() + timedelta(hours=i - 3)) if i % 2 == 0 else None), ['website', 'manual', 'csv'][i % 3], user.id)
            lead.created_at = now() - timedelta(hours=i * 4)
        db.commit()
    credentials.write_text(f'DEMO_EMAIL=demo@timely.local\nDEMO_PASSWORD={password}\nORGANIZATION=timely-health\n', encoding='utf8')
    print('Synthetic demo records created. Credentials saved to backend/.env.demo.local; do not deploy this database.')


if __name__ == '__main__':
    main()
