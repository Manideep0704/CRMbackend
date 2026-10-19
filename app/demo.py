"""Create only synthetic development records. Never run against production."""
import os
import secrets
from datetime import timedelta
from pathlib import Path
from .db import get_crm_db, now
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
    db = get_crm_db()
    org = db.organizations.find_one({'slug': 'timely-health'})
    user = db.users.find_one({'email': 'demo@timely.local'})
    org_id = str(org['_id']) if org else 'timely-health'
    user_id = str(user['_id']) if user else None
    website = db.websites.find_one({'organization_id': org_id})
    website_id = str(website['_id']) if website else None

    names = ['Aarav Shah', 'Meera Iyer', 'Rohan Verma', 'Ananya Rao', 'Kabir Mehta', 'Sara Thomas', 'Vivaan Patel', 'Diya Kapoor', 'Arjun Nair', 'Ishita Sen', 'Aditya Rao', 'Nisha Das']
    states = ['new', 'qualified', 'contacted', 'converted', 'new', 'qualified', 'contacted', 'lost', 'new', 'converted', 'new', 'contacted']
    for i, name in enumerate(names):
        lead, _ = create_lead(db, org_id, LeadInput(full_name=name, email=f'sample{i+1}@example.com', company_name=['Sample Clinic', 'Example Wellness', 'Demo Health Co.'][i % 3], inquiry_type='Digital consultation', status=states[i], priority=['medium', 'high', 'low'][i % 3], website_id=website_id if i % 3 == 0 else None, assigned_to=user_id, next_follow_up_at=(now() + timedelta(hours=i - 3)) if i % 2 == 0 else None), ['website', 'manual', 'csv'][i % 3], user_id)
        db.leads.update_one({'_id': lead['_id']}, {'$set': {'created_at': now() - timedelta(hours=i * 4)}})
    credentials.write_text(f'DEMO_EMAIL=demo@timely.local\nDEMO_PASSWORD={password}\nORGANIZATION=timely-health\n', encoding='utf8')
    print('Synthetic demo records created in MongoDB. Credentials saved to backend/.env.demo.local; do not deploy this database.')


if __name__ == '__main__':
    main()
