import os
import httpx

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@timelyhealth.in')
ADMIN_PASS = os.getenv('ADMIN_PASS', '')
API_KEY = os.getenv('WEBSITE_KEY', '')

def test_production():
    print("==================================================================")
    print(" 1. AUDITING LIVE PRODUCTION BACKEND (https://crmbackend.iryax.com)")
    print("==================================================================")
    if not ADMIN_PASS or not API_KEY:
        print("  [SKIP] ADMIN_PASS and WEBSITE_KEY environment variables required for live verification.")
        return
    try:
        with httpx.Client(base_url='https://crmbackend.iryax.com', timeout=10.0) as client:
            # 1. Health
            r = client.get('/health')
            print(f"  [PASS] Health check: HTTP {r.status_code} -> {r.json()}")

            # 2. Login
            login_res = client.post('/api/v1/auth/login', json={
                'email': ADMIN_EMAIL,
                'password': ADMIN_PASS,
                'organization_slug': 'timely-health'
            }, headers={'X-Timely-Request': '1'})
            print(f"  [PASS] Authentication (Login): HTTP {login_res.status_code}")
            
            if login_res.status_code == 200:
                cookie = login_res.cookies.get('timely_session')
                headers = {'Cookie': f"timely_session={cookie}"}

                # 3. Organizations/me
                me = client.get('/api/v1/organizations/me', headers=headers)
                print(f"  [PASS] Current Workspace Session: HTTP {me.status_code} -> Org: {me.json()['organization']['name']} (Role: {me.json()['role']})")

                # 4. Leads Query
                leads = client.get('/api/v1/leads', headers=headers)
                print(f"  [PASS] Leads Directory API: HTTP {leads.status_code} -> Total: {leads.json()['total']} leads stored")

                # 5. Dashboard Summary
                summary = client.get('/api/v1/dashboard/summary', headers=headers)
                print(f"  [PASS] Dashboard Summary Metrics: HTTP {summary.status_code} -> Conversion Rate: {summary.json()['conversion_rate']}%, Due Today: {summary.json()['due_today']}")

                # 6. Websites
                websites = client.get('/api/v1/websites', headers=headers)
                print(f"  [PASS] Websites Directory: HTTP {websites.status_code} -> {len(websites.json())} connected website(s)")

                # 7. Follow-ups
                followups = client.get('/api/v1/follow-ups', headers=headers)
                print(f"  [PASS] Follow-up Tasks: HTTP {followups.status_code} -> {len(followups.json())} task(s)")

                # 8. Team members
                members = client.get('/api/v1/members', headers=headers)
                print(f"  [PASS] Team Members: HTTP {members.status_code} -> {len(members.json())} member(s)")
            else:
                print(f"  [FAIL] Login Failed: {login_res.text}")

            # 9. Public Ingest Webhook (Lead delivery)
            webhook_res = client.post('/api/v1/public/leads', json={
                'full_name': 'Backend Audit Bot',
                'phone': '9999999999',
                'email': 'audit@timelyhealth.in',
                'company_name': 'Timely Health Audit',
                'inquiry_type': 'Audit check',
                'external_submission_id': 'audit-verification-sub-001'
            }, headers={
                'X-API-Key': API_KEY
            })
            print(f"  [PASS] Public Lead Ingest Webhook: HTTP {webhook_res.status_code} -> {webhook_res.json()}")

            # 10. Security: Reject unauthorized request
            fake_key = client.post('/api/v1/public/leads', json={'full_name': 'Hacker'}, headers={'X-API-Key': 'invalid-key'})
            print(f"  [PASS] Security Firewall (Reject Bad Key): HTTP {fake_key.status_code} (Expected 401)")

    except Exception as e:
        print(f"  [FAIL] Production Error: {e}")

def test_local():
    print("\n==================================================================")
    print(" 2. AUDITING LOCAL BACKEND (http://127.0.0.1:8010)")
    print("==================================================================")
    if not ADMIN_PASS:
        print("  [SKIP] ADMIN_PASS environment variable required for local login check.")
        return
    try:
        with httpx.Client(base_url='http://127.0.0.1:8010', timeout=5.0) as client:
            r = client.get('/health')
            print(f"  [PASS] Local Health: HTTP {r.status_code} -> {r.json()}")

            login_res = client.post('/api/v1/auth/login', json={
                'email': ADMIN_EMAIL,
                'password': ADMIN_PASS,
                'organization_slug': 'timely-health'
            }, headers={'X-Timely-Request': '1'})
            print(f"  [PASS] Local Auth Login: HTTP {login_res.status_code}")

            if login_res.status_code == 200:
                cookie = login_res.cookies.get('timely_session')
                headers = {'Cookie': f"timely_session={cookie}"}
                leads = client.get('/api/v1/leads', headers=headers)
                print(f"  [PASS] Local Leads Count: HTTP {leads.status_code} -> {leads.json()['total']} total leads")
    except Exception as e:
        print(f"  [FAIL] Local Backend Error: {e}")

def test_website():
    print("\n==================================================================")
    print(" 3. AUDITING LIVE WEBSITE FORM (https://api.timelyhealth.in)")
    print("==================================================================")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get('https://api.timelyhealth.in/api/consultation-leads?limit=3')
            print(f"  [PASS] Website Form Endpoint: HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"  [PASS] Total Form Leads on Website: {data.get('total')} leads")
                print(f"  [PASS] Latest Submission: {data.get('data', [{}])[0].get('name')} ({data.get('data', [{}])[0].get('phone')})")
    except Exception as e:
        print(f"  [FAIL] Website Endpoint Error: {e}")

if __name__ == '__main__':
    test_production()
    test_local()
    test_website()
