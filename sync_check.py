import httpx
from app.db import get_crm_db
from app.schemas import LeadInput
from app.services import create_lead

res = httpx.get('https://api.timelyhealth.in/api/consultation-leads?limit=50', timeout=10)
print('Website API status:', res.status_code)
data = res.json().get('data', [])
print('Found leads:', len(data))

db = get_crm_db()
org = db['organizations'].find_one({'slug': 'timely-health'})
website = db['websites'].find_one({'domain': 'digital.timelyhealth.in'})

org_id = org.get('id') or str(org.get('_id')) if org else None
website_id = website.get('id') or str(website.get('_id')) if website else None

print('Org:', org_id)
print('Website:', website_id)

if org_id and website_id:
    for item in data:
        ext_id = f"timely-consultation:{item['_id']}"
        existing = db['leads'].find_one({
            'organization_id': org_id,
            'external_submission_id': ext_id
        })
        if not existing:
            print('Inserting lead:', item.get('name'))
            try:
                lead_input = LeadInput(
                    full_name=item.get('name') or 'Website Lead',
                    email=item.get('email') or '',
                    phone=item.get('phone') or '',
                    company_name=item.get('businessName') or '',
                    inquiry_type=item.get('lookingFor') or 'Digital consultation',
                    website_id=website_id
                )
                attribution = {
                    'business_type': item.get('businessType') or '',
                    'form_id': 'timely-digital-consultation'
                }
                create_lead(db, org_id, lead_input, 'website', external_id=ext_id, attribution=attribution)
            except Exception as e:
                print('Error inserting lead:', e)
        else:
            print('Lead already exists:', item.get('name'))
