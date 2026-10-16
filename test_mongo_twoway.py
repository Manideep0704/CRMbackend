from app.mongo import update_lead_in_mongo, get_mongo_db

db = get_mongo_db()
doc = db.consultationleads.find_one({'name': 'Rohith'})
if doc:
    ext_id = f"timely-consultation:{doc['_id']}"
    print("Found Rohith in MongoDB. Current status:", doc.get("status"))
    
    # 1. Update to Contacted
    update_lead_in_mongo(ext_id, "Contacted")
    after = db.consultationleads.find_one({'_id': doc['_id']})
    print("Status in MongoDB after update:", after.get("status"))
    assert after.get("status") == "Contacted"
    
    # 2. Restore to New
    update_lead_in_mongo(ext_id, "New")
    restored = db.consultationleads.find_one({'_id': doc['_id']})
    print("Restored status in MongoDB:", restored.get("status"))
    print("\nSUCCESS: Two-way MongoDB write & update verified!")
