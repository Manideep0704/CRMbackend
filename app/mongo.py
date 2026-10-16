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


def authenticate_attendance_user(identifier: str, password: str):
    """
    Validates credentials against attendanceDB in MongoDB.
    Allows admins and employees from attendanceDB to access this CRM tool.
    Supports login via email, mobile/phone, or employeeId.
    """
    db_mongo = get_mongo_db()
    if db_mongo is None or not identifier or not password:
        return None
    try:
        import re
        ident = identifier.strip()
        ident_pattern = {'$regex': f'^{re.escape(ident)}$', '$options': 'i'}

        # 1. Check 'admins' collection
        admin = db_mongo['admins'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'mobile': ident},
                {'name': ident_pattern}
            ]
        })
        if admin and str(admin.get('password')) == str(password):
            return {
                'email': (admin.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': admin.get('name') or 'Admin',
                'role': 'admin'
            }

        # 2. Check 'employees' collection
        emp = db_mongo['employees'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'employeeId': ident_pattern},
                {'phone': ident}
            ]
        })
        if emp and str(emp.get('password')) == str(password):
            emp_role = str(emp.get('role', '')).lower()
            emp_dept = str(emp.get('department', '')).lower()
            if any(k in emp_role for k in ['admin', 'director', 'general manager']):
                crm_role = 'admin'
            elif 'sales' in emp_dept:
                crm_role = 'agent'
            else:
                crm_role = 'viewer' # Only sales department persons become agents!
            return {
                'email': (emp.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': emp.get('name') or 'Employee',
                'role': crm_role
            }

        # 3. Check 'empls' collection
        empl = db_mongo['empls'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'phone': ident}
            ]
        })
        if empl and str(empl.get('password')) == str(password):
            empl_dept = str(empl.get('department', '')).lower()
            crm_role = 'agent' if 'sales' in empl_dept else 'viewer'
            return {
                'email': (empl.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': empl.get('name') or 'Employee',
                'role': crm_role
            }
    except Exception as e:
        print(f"[MongoDB Auth] Error checking attendanceDB user: {e}")
    return None


def sync_sales_agents_from_mongo():
    """
    Syncs sales department employees from attendanceDB into CRM members as agents.
    Ensures ONLY sales department employees hold the 'agent' role.
    """
    db_mongo = get_mongo_db()
    if db_mongo is None:
        return 0

    try:
        from .models import User, Membership, Organization
        from .security import hash_password

        sales_emps = list(db_mongo['employees'].find({'department': {'$regex': 'sales', '$options': 'i'}}))
        sales_emails = set()

        with SessionLocal() as db_crm:
            org = db_crm.scalar(select(Organization).where(Organization.slug == 'timely-health'))
            if not org:
                return 0

            for emp in sales_emps:
                email = (emp.get('email') or '').lower().strip()
                if not email:
                    continue
                sales_emails.add(email)
                name = (emp.get('name') or 'Sales Agent').strip()
                pwd = str(emp.get('password') or 'Sales@123')

                user = db_crm.scalar(select(User).where(User.email == email))
                if not user:
                    user = User(
                        name=name,
                        email=email,
                        password_hash=hash_password(pwd),
                        is_active=True
                    )
                    db_crm.add(user)
                    db_crm.flush()
                else:
                    if name and not user.name:
                        user.name = name
                    user.is_active = True
                    db_crm.flush()

                membership = db_crm.get(Membership, (org.id, user.id))
                if not membership:
                    membership = Membership(
                        organization_id=org.id,
                        user_id=user.id,
                        role='agent',
                        status='active'
                    )
                    db_crm.add(membership)
                else:
                    membership.role = 'agent'
                    membership.status = 'active'
                db_crm.flush()

            # Ensure any non-sales users currently marked as 'agent' are downgraded to 'viewer'
            memberships = db_crm.scalars(
                select(Membership).where(
                    Membership.organization_id == org.id,
                    Membership.role == 'agent'
                )
            ).all()
            for m in memberships:
                u = db_crm.get(User, m.user_id)
                if u and u.email not in sales_emails:
                    m.role = 'viewer'
                    print(f"[Sales Sync] Updated non-sales user {u.name} ({u.email}) from agent to viewer.")

            db_crm.commit()
            print(f"[Sales Sync] Synced {len(sales_emails)} Sales Department agents from MongoDB attendanceDB.")
            return len(sales_emails)
    except Exception as e:
        print(f"[Sales Sync] Error: {e}")
        return 0


