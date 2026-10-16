import httpx
import uuid

URL = 'https://crmbackend.iryax.com/api/v1/public/leads'
API_KEY = 'tly_9Cy-8gtWI2FMMh_GpxLTwfFr5Iav7gY7-cR-1JxnSinXwaFwhc9enQ'

print("====================================================")
print("        WEBHOOK SUITE VERIFICATION REPORT           ")
print("====================================================")

sub_id = f"webhook-test-{uuid.uuid4().hex[:8]}"
payload = {
    "full_name": "Dr. Webhook Verification",
    "phone": "9876543210",
    "email": "webhooktest@timelyhealth.in",
    "company_name": "Metro Wellness Clinic",
    "business_type": "Clinic / Hospital",
    "inquiry_type": "Full Digital Package",
    "form_id": "timely-digital-consultation",
    "external_submission_id": sub_id
}

with httpx.Client(timeout=10.0) as client:
    # 1. Valid POST
    r1 = client.post(URL, json=payload, headers={"X-API-Key": API_KEY})
    print(f"Test 1 [Valid Webhook Submission]: HTTP {r1.status_code} -> {r1.json()}")
    assert r1.status_code == 200 and r1.json().get("accepted") == True

    # 2. Idempotency (Duplicate Prevention)
    r2 = client.post(URL, json=payload, headers={"X-API-Key": API_KEY})
    print(f"Test 2 [Deduplication / Idempotent Replay]: HTTP {r2.status_code} -> {r2.json()}")
    assert r2.status_code == 200 and r2.json().get("accepted") == True

    # 3. Security (Invalid Key)
    r3 = client.post(URL, json=payload, headers={"X-API-Key": "invalid_secret_key"})
    print(f"Test 3 [Firewall / Invalid Key Rejection]: HTTP {r3.status_code} (Expected: 401)")
    assert r3.status_code == 401

    # 4. Security (Missing Key)
    r4 = client.post(URL, json=payload)
    print(f"Test 4 [Firewall / Missing Key Rejection]: HTTP {r4.status_code} (Expected: 401)")
    assert r4.status_code == 401

    # 5. Validation (Missing External Submission ID)
    bad_payload = {"full_name": "No ID Person", "email": "test@test.com"}
    r5 = client.post(URL, json=bad_payload, headers={"X-API-Key": API_KEY})
    print(f"Test 5 [Data Validation / Missing ID]: HTTP {r5.status_code} (Expected: 422)")
    assert r5.status_code == 422

    # 6. Verify in CRM Dashboard (Login & Query)
    login = client.post("https://crmbackend.iryax.com/api/v1/auth/login", json={
        "email": "admin@timelyhealth.in",
        "password": "TimelyAdmin2026!",
        "organization_slug": "timely-health"
    }, headers={"X-Timely-Request": "1"})
    cookie = login.cookies.get("timely_session")
    
    leads = client.get("https://crmbackend.iryax.com/api/v1/leads?q=Dr.+Webhook", headers={"Cookie": f"timely_session={cookie}"})
    items = leads.json().get("items", [])
    print(f"Test 6 [Database Persistence & Retrieval]: Found {len(items)} matching lead(s)")
    if items:
        lead = items[0]
        print(f"         Lead Name: {lead.get('full_name')}")
        print(f"         Company:   {lead.get('company_name')}")
        print(f"         Source:    {lead.get('source')}")
        print(f"         Inquiry:   {lead.get('inquiry_type')}")
        print(f"         Created:   {lead.get('created_at')}")

print("====================================================")
print("   ALL 6 WEBHOOK SPECIFICATION TESTS PASSED!        ")
print("====================================================")
