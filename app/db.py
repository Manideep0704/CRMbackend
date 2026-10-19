import os
from datetime import datetime, timezone
from uuid import uuid4
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

DEFAULT_MONGO_URI = 'mongodb+srv://saquiba:saquiba123@cluster0.hrgeeif.mongodb.net/attendanceDB?retryWrites=true&w=majority&appName=attendance'
MONGO_URI = os.getenv('MONGO_URI') or DEFAULT_MONGO_URI

_client = None

def get_mongo_client():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    return _client

def get_crm_db():
    client = get_mongo_client()
    return client['crmDB']

def get_attendance_db():
    client = get_mongo_client()
    return client['attendanceDB']

def get_db():
    """FastAPI dependency for accessing CRM MongoDB database."""
    return get_crm_db()

def now():
    return datetime.now(timezone.utc)

def uid():
    return str(uuid4())

def init_mongo_indexes():
    """Creates indexes on CRM collections for performance and uniqueness."""
    try:
        db = get_crm_db()
        db['organizations'].create_index([('slug', ASCENDING)], unique=True)
        db['users'].create_index([('email', ASCENDING)], unique=True)
        db['users'].create_index([('auth_subject', ASCENDING)])
        db['memberships'].create_index([('organization_id', ASCENDING), ('user_id', ASCENDING)], unique=True)
        db['websites'].create_index([('organization_id', ASCENDING), ('domain', ASCENDING)])
        db['website_keys'].create_index([('key_hash', ASCENDING)], unique=True)
        db['leads'].create_index([('organization_id', ASCENDING), ('created_at', DESCENDING)])
        db['leads'].create_index([('organization_id', ASCENDING), ('status', ASCENDING)])
        db['leads'].create_index([('website_id', ASCENDING), ('external_submission_id', ASCENDING)])
        db['auth_sessions'].create_index([('token_hash', ASCENDING)], unique=True)
        db['auth_sessions'].create_index([('user_id', ASCENDING)])
        db['activities'].create_index([('organization_id', ASCENDING), ('lead_id', ASCENDING)])
        db['notes'].create_index([('organization_id', ASCENDING), ('lead_id', ASCENDING)])
        db['follow_ups'].create_index([('organization_id', ASCENDING), ('lead_id', ASCENDING)])
        print("[MongoDB] CRM indexes ensured successfully.")
    except Exception as e:
        print(f"[MongoDB] Index creation notice: {e}")
