import os
import re
import secrets as _sec
from datetime import datetime, timezone

from .db import get_attendance_db, get_crm_db, uid
from .models import Membership, ModelDict, User
from .schemas import LeadInput
from .security import hash_password, revoke_user_sessions
from .services import create_lead

MONGO_URI = os.getenv('MONGO_URI', '')


def get_mongo_db():
    return get_attendance_db()


def sync_leads_from_mongo():
    """Reads all consultation leads directly from company MongoDB and imports any new ones."""
    db_mongo = get_attendance_db()
    if db_mongo is None:
        return 0

    try:
        leads_col = db_mongo['consultationleads']
        docs = list(leads_col.find().sort('createdAt', -1).limit(100))
        if not docs:
            return 0

        synced_count = 0
        db_crm = get_crm_db()
        org = db_crm['organizations'].find_one({'slug': 'timely-health'})
        website = db_crm['websites'].find_one({'domain': 'digital.timelyhealth.in'})
        if not org or not website:
            return 0

        org_id = org['id']
        website_id = website['id']

        for doc in docs:
            ext_id = f"timely-consultation:{doc['_id']}"
            existing = db_crm['leads'].find_one({
                'organization_id': org_id,
                'external_submission_id': ext_id
            })
            if not existing:
                lead_input = LeadInput(
                    full_name=doc.get('name') or 'Website Lead',
                    email=doc.get('email') or '',
                    phone=doc.get('phone') or '',
                    company_name=doc.get('businessName') or '',
                    inquiry_type=doc.get('lookingFor') or 'Digital consultation',
                    website_id=website_id
                )
                attribution = {
                    'business_type': doc.get('businessType') or '',
                    'form_id': 'timely-digital-consultation',
                    'ingest_channel': 'company_mongodb_direct'
                }
                create_lead(db_crm, org_id, lead_input, 'website', external_id=ext_id, attribution=attribution)
                synced_count += 1

        if synced_count > 0:
            print(f"[MongoDB Direct Sync] Synced {synced_count} new lead(s) from MongoDB consultationleads.")
        return synced_count
    except Exception as e:
        print(f"[MongoDB] Sync error: {e}")
        return 0


def update_lead_in_mongo(external_id: str, new_status: str):
    """Updates lead status back to company MongoDB."""
    db_mongo = get_attendance_db()
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
    db_mongo = get_attendance_db()
    if db_mongo is None or not identifier or not password:
        return None
    try:
        ident = identifier.strip()
        escaped_ident = re.escape(ident)
        ident_pattern = {'$regex': f'^{escaped_ident}$', '$options': 'i'}

        # 1. Check 'admins' collection
        admin = db_mongo['admins'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'mobile': ident},
                {'name': ident_pattern}
            ]
        })
        if admin and _sec.compare_digest(str(admin.get('password') or ''), str(password or '')):
            admin_status = str(admin.get('status', 'active')).strip().lower()
            if admin_status != 'active':
                print(f"[MongoDB Auth] Admin {admin.get('name')} is inactive in attendanceDB. Denying login.")
                return None
            return {
                '_id': str(admin.get('_id', '')),
                'email': (admin.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': admin.get('name') or 'Admin',
                'role': 'admin',
                'status': admin_status,
                'entitled_org_slug': 'timely-health'
            }

        # 2. Check 'employees' collection
        emp = db_mongo['employees'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'employeeId': ident_pattern},
                {'phone': ident}
            ]
        })
        if emp and _sec.compare_digest(str(emp.get('password') or ''), str(password or '')):
            emp_status = str(emp.get('status', 'active')).strip().lower()
            if emp_status != 'active':
                print(f"[MongoDB Auth] Employee {emp.get('name')} is inactive in attendanceDB. Denying login.")
                return None
            emp_role = str(emp.get('role', '')).lower()
            emp_dept = str(emp.get('department', '')).lower()
            if any(k in emp_role for k in ['admin', 'director', 'general manager']):
                crm_role = 'admin'
            elif 'sales' in emp_dept:
                crm_role = 'agent'
            else:
                crm_role = 'viewer'
            return {
                '_id': str(emp.get('_id', '')),
                'email': (emp.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': emp.get('name') or 'Employee',
                'role': crm_role,
                'status': emp_status,
                'entitled_org_slug': 'timely-health'
            }

        # 3. Check 'empls' collection
        empl = db_mongo['empls'].find_one({
            '$or': [
                {'email': ident_pattern},
                {'phone': ident}
            ]
        })
        if empl and _sec.compare_digest(str(empl.get('password') or ''), str(password or '')):
            empl_status = str(empl.get('status', 'active')).strip().lower()
            if empl_status != 'active':
                print(f"[MongoDB Auth] Empl {empl.get('name')} is inactive in attendanceDB. Denying login.")
                return None
            empl_dept = str(empl.get('department', '')).lower()
            crm_role = 'agent' if 'sales' in empl_dept else 'viewer'
            return {
                '_id': str(empl.get('_id', '')),
                'email': (empl.get('email') or f'{ident}@timelyhealth.in').lower(),
                'name': empl.get('name') or 'Employee',
                'role': crm_role,
                'status': empl_status,
                'entitled_org_slug': 'timely-health'
            }
    except Exception as e:
        print(f"[MongoDB Auth] Error checking attendanceDB user: {e}")
    return None


def check_attendance_user_status(email: str) -> tuple[bool, bool]:
    """
    R4 – Fail-closed directory status check.
    Returns (exists_in_directory, is_active).
    """
    db_mongo = get_attendance_db()
    if db_mongo is None:
        return False, False
    try:
        email_clean = email.lower().strip()
        escaped_email = re.escape(email_clean)
        pattern = {'$regex': f'^{escaped_email}$', '$options': 'i'}

        for coll in ['admins', 'employees', 'empls']:
            user = db_mongo[coll].find_one({'email': pattern})
            if user:
                status = str(user.get('status', 'active')).strip().lower()
                return True, status == 'active'
        return False, True
    except Exception as e:
        print(f"[MongoDB Auth] Directory status check failed: {e}")
        return False, False


def sync_sales_agents_from_mongo():
    """Syncs active sales department employees into CRM users with 'agent' role."""
    db_mongo = get_attendance_db()
    if db_mongo is None:
        return 0

    try:
        active_sales_emails = set()
        active_sales_emps = list(db_mongo['employees'].find({
            'department': {'$regex': 'sales', '$options': 'i'},
            'status': {'$regex': '^active$', '$options': 'i'}
        }))
        active_sales_empls = list(db_mongo['empls'].find({
            'department': {'$regex': 'sales', '$options': 'i'},
            'status': {'$regex': '^active$', '$options': 'i'}
        }))
        active_sales_emps.extend(active_sales_empls)

        inactive_sales_emps = list(db_mongo['employees'].find({
            'department': {'$regex': 'sales', '$options': 'i'},
            'status': {'$regex': '^(?!active$).*$', '$options': 'i'}
        }))
        inactive_admins = list(db_mongo['admins'].find({
            'status': {'$regex': '^(?!active$).*$', '$options': 'i'}
        }))
        inactive_empls = list(db_mongo['empls'].find({
            'status': {'$regex': '^(?!active$).*$', '$options': 'i'}
        }))
        inactive_emails = {
            (e.get('email') or '').lower().strip()
            for e in (inactive_sales_emps + inactive_admins + inactive_empls)
            if (e.get('email') or '').strip()
        }

        db_crm = get_crm_db()
        org = db_crm['organizations'].find_one({'slug': 'timely-health'})
        if not org:
            return 0
        org_id = org['id']

        for emp in active_sales_emps:
            email = (emp.get('email') or '').lower().strip()
            if not email:
                continue
            active_sales_emails.add(email)
            name = (emp.get('name') or 'Sales Agent').strip()
            random_hash = hash_password(_sec.token_urlsafe(32))
            subject_id = str(emp.get('_id', '')).strip()

            user_by_sub = db_crm['users'].find_one({'auth_provider': 'attendance', 'auth_subject': subject_id}) if subject_id else None
            user_by_email = db_crm['users'].find_one({'email': email})

            if user_by_sub and user_by_email and user_by_sub['id'] != user_by_email['id']:
                print(f"[Sales Sync] Skipping {email}: subject {subject_id} collision with different user.")
                continue

            if user_by_sub:
                user_id = user_by_sub['id']
                db_crm['users'].update_one(
                    {'_id': user_by_sub['_id']},
                    {'$set': {'email': email, 'name': name or user_by_sub.get('name'), 'password_hash': random_hash}}
                )
                user_is_active = user_by_sub.get('is_active', True)
            elif user_by_email:
                if user_by_email.get('auth_provider') == 'local':
                    print(f"[Sales Sync] Skipping {email}: matches local account.")
                    continue
                user_id = user_by_email['id']
                db_crm['users'].update_one(
                    {'_id': user_by_email['_id']},
                    {'$set': {'auth_provider': 'attendance', 'auth_subject': subject_id, 'password_hash': random_hash}}
                )
                user_is_active = user_by_email.get('is_active', True)
            else:
                new_user = User(
                    name=name,
                    email=email,
                    password_hash=random_hash,
                    is_active=True,
                    auth_provider='attendance',
                    auth_subject=subject_id
                )
                db_crm['users'].insert_one(new_user)
                user_id = new_user['id']
                user_is_active = True

            if not user_is_active:
                continue

            mem_id = f"{org_id}:{user_id}"
            db_crm['memberships'].update_one(
                {'$or': [{'_id': mem_id}, {'organization_id': org_id, 'user_id': user_id}]},
                {'$set': {'_id': mem_id, 'id': mem_id, 'organization_id': org_id, 'user_id': user_id, 'role': 'agent', 'status': 'active'}},
                upsert=True
            )

        for inact_email in inactive_emails:
            u = db_crm['users'].find_one({'email': inact_email})
            if u and u.get('auth_provider') == 'attendance':
                u_id = u['id']
                db_crm['users'].update_one({'_id': u['_id']}, {'$set': {'is_active': False}})
                revoke_user_sessions(db_crm, u_id)
                db_crm['leads'].update_many(
                    {'organization_id': org_id, 'assigned_to': u_id},
                    {'$set': {'assigned_to': None}}
                )
                db_crm['memberships'].update_one(
                    {'$or': [{'_id': f"{org_id}:{u_id}"}, {'organization_id': org_id, 'user_id': u_id}]},
                    {'$set': {'status': 'inactive'}}
                )

        # Update non-sales users currently marked as agent to viewer
        members = list(db_crm['memberships'].find({'organization_id': org_id, 'role': 'agent'}))
        for m in members:
            u = db_crm['users'].find_one({'$or': [{'_id': m['user_id']}, {'id': m['user_id']}]})
            if u and u.get('email') not in active_sales_emails:
                db_crm['memberships'].update_one({'_id': m['_id']}, {'$set': {'role': 'viewer'}})

        print(f"[Sales Sync] Synced {len(active_sales_emails)} ACTIVE Sales Department agents to MongoDB crmDB.")
        return len(active_sales_emails)
    except Exception as e:
        print(f"[Sales Sync] Error: {e}")
        return 0
