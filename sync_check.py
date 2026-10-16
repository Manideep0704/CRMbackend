import httpx
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Organization, Website, Lead
from app.schemas import LeadInput
from app.services import create_lead

res = httpx.get('https://api.timelyhealth.in/api/consultation-leads?limit=50', timeout=10)
print('Website API status:', res.status_code)
data = res.json().get('data', [])
print('Found leads:', len(data))

with SessionLocal() as db:
    org = db.scalar(select(Organization).where(Organization.slug == 'timely-health'))
    website = db.scalar(select(Website).where(Website.domain == 'digital.timelyhealth.in'))
    print('Org:', org.id if org else None)
    print('Website:', website.id if website else None)
    
    for item in data:
        ext_id = f"timely-consultation:{item['_id']}"
        existing = db.scalar(select(Lead).where(
            Lead.organization_id == org.id,
            Lead.external_submission_id == ext_id
        ))
        if not existing:
            print('Inserting lead:', item.get('name'))
            try:
                lead_input = LeadInput(
                    full_name=item.get('name') or 'Website Lead',
                    email=item.get('email') or None,
                    phone=item.get('phone') or None,
                    company_name=item.get('businessName') or None,
                    inquiry_type=item.get('lookingFor') or 'Digital consultation',
                    website_id=website.id
                )
                attribution = {
                    'business_type': item.get('businessType') or '',
                    'form_id': 'timely-digital-consultation'
                }
                create_lead(db, org.id, lead_input, 'website', external_id=ext_id, attribution=attribution)
                print('  -> Inserted!')
            except Exception as e:
                print('  -> Error inserting:', e)
        else:
            print('Already exists:', item.get('name'), ext_id)
    db.commit()
    print('Done commit!')
