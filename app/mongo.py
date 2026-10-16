import os
from datetime import datetime, timezone
from pymongo import MongoClient
from sqlalchemy import select
from .db import SessionLocal
from .models import Organization, Website, Lead
from .schemas import LeadInput
from .services import create_lead

MONGO_URI = os.getenv(
    'MONGO_URI',
    'mongodb+srv://saquiba:saquiba123@cluster0.hrgeeif.mongodb.net/attendanceDB?retryWrites=true&w=majority&appName=attendance'
)

def get_mongo_db():
    if not MONGO_URI:
        return None
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        try:
            db = client.get_default_database()
            if db is not None:
                return db
        except Exception:
            pass
        return client['attendanceDB']
    except Exception as e:
        print(f"[MongoDB] Connection error: {e}")
        return None

def sync_leads_from_mongo():
    """Reads all consultation leads directly from company MongoDB and imports any new ones."""
    db_mongo = get_mongo_db()
    if db_mongo is None:
        return 0

    try:
        leads_col = db_mongo['consultationleads']
        docs = list(leads_col.find().sort('createdAt', -1).limit(100))
        if not docs:
            return 0

        synced_count = 0
        with SessionLocal() as db_crm:
            org = db_crm.scalar(select(Organization).where(Organization.slug == 'timely-health'))
            website = db_crm.scalar(select(Website).where(Website.domain == 'digital.timelyhealth.in'))
            if not org or not website:
                return 0

            for doc in docs:
                ext_id = f"timely-consultation:{doc['_id']}"
                existing = db_crm.scalar(select(Lead).where(
                    Lead.organization_id == org.id,
                    Lead.external_submission_id == ext_id
                ))
                if not existing:
                    lead_input = LeadInput(
                        full_name=doc.get('name') or 'Website Lead',
                        email=doc.get('email') or '',
                        phone=doc.get('phone') or '',
                        company_name=doc.get('businessName') or '',
                        inquiry_type=doc.get('lookingFor') or 'Digital consultation',
                        website_id=website.id
                    )
                    attribution = {
                        'business_type': doc.get('businessType') or '',
                        'form_id': 'timely-digital-consultation',
                        'ingest_channel': 'company_mongodb_direct'
                    }
                    create_lead(db_crm, org.id, lead_input, 'website', external_id=ext_id, attribution=attribution)
                    synced_count += 1

            if synced_count > 0:
                db_crm.commit()
                print(f"[MongoDB Direct Sync] Synced {synced_count} new lead(s) from MongoDB consultationleads.")
        return synced_count
    except Exception as e:
        print(f"[MongoDB] Sync error: {e}")
        return 0

def update_lead_in_mongo(external_id: str, new_status: str):
    """Updates lead status back to company MongoDB."""
    db_mongo = get_mongo_db()
    if db_mongo is None or not external_id:
        return False
    try:
        from bson import ObjectId
        raw_id = external_id.replace("timely-consultation:", "")
        if ObjectId.is_valid(raw_id):
            db_mongo['consultationleads'].update_one(
                {'_id': ObjectId(raw_id)},
                {'$set': {'status': new_status, 'updatedAt': datetime.now(timezone.utc)}}
            )
            return True
    except Exception as e:
        print(f"[MongoDB] Failed to update lead in MongoDB: {e}")
    return False
