from fastapi.testclient import TestClient
from app.main import app
import uuid

client = TestClient(app)

def test_lead_acceptance_and_diversion():
    headers = {"X-Timely-Request": "1", "origin": "http://localhost:3000"}

    # 1. Login Admin (Saidulu Reddy)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": "saidulureddy@gmail.com", "password": "reddy123", "organization_slug": "timely-health"},
        headers=headers
    )
    assert admin_login.status_code == 200, admin_login.text
    admin_token = admin_login.cookies.get("timely_session")

    # 2. Login Agent 1 (Naresh Kumar)
    agent_login = client.post(
        "/api/v1/auth/login",
        json={"email": "nareshyadav5313@gmail.com", "password": "naresh@123", "organization_slug": "timely-health"},
        headers=headers
    )
    assert agent_login.status_code == 200, agent_login.text
    agent_token = agent_login.cookies.get("timely_session")

    # 3. Create a test website lead via website integration
    sites = client.get("/api/v1/websites", cookies={"timely_session": admin_token}, headers={"origin": "http://localhost:3000"}).json()
    assert len(sites) > 0, "At least one website should exist"
    website_id = sites[0]["id"]
    rot = client.post(f"/api/v1/websites/{website_id}/rotate-key", cookies={"timely_session": admin_token}, headers=headers)
    assert rot.status_code == 200, rot.text
    api_key = rot.json()["key"]

    sub_id = f"test-sub-{uuid.uuid4().hex[:8]}"
    pub_res = client.post(
        "/api/v1/public/leads",
        json={
            "full_name": "Test Accept Lead",
            "phone": "9876543210",
            "email": f"test_{uuid.uuid4().hex[:6]}@example.com",
            "external_submission_id": sub_id
        },
        headers={"x-api-key": api_key, "Idempotency-Key": sub_id}
    )
    assert pub_res.status_code == 200, pub_res.text

    # 4. Check summary shows unaccepted lead
    summary = client.get("/api/v1/dashboard/summary", cookies={"timely_session": agent_token}, headers={"origin": "http://localhost:3000"}).json()
    assert summary["unaccepted_count"] >= 1
    print("Unaccepted count in summary:", summary["unaccepted_count"])

    # Find the created lead
    leads = client.get("/api/v1/leads?q=Test Accept Lead", cookies={"timely_session": agent_token}, headers={"origin": "http://localhost:3000"}).json()["items"]
    target_lead = leads[0]
    lead_id = target_lead["id"]
    assert target_lead["accepted_by"] is None, "New website lead must be unaccepted"

    # 5. Agent 1 accepts the lead
    accept_res = client.post(f"/api/v1/leads/{lead_id}/accept", cookies={"timely_session": agent_token}, headers=headers)
    assert accept_res.status_code == 200, accept_res.text
    accepted_data = accept_res.json()
    assert accepted_data["accepted_by"] is not None
    assert accepted_data["assigned_to"] is not None
    print("Lead successfully accepted by agent:", accepted_data["accepted_by"])

    # 6. Agent 1 attempts to divert / reassign this accepted lead to someone else -> MUST FAIL with 403 Forbidden!
    members = client.get("/api/v1/members", cookies={"timely_session": admin_token}, headers={"origin": "http://localhost:3000"}).json()
    other_member_id = [m["id"] for m in members if m["id"] != accepted_data["accepted_by"]][0]

    agent_divert = client.patch(
        f"/api/v1/leads/{lead_id}",
        json={"assigned_to": other_member_id},
        cookies={"timely_session": agent_token},
        headers=headers
    )
    print("Agent divert attempt status:", agent_divert.status_code, agent_divert.text)
    assert agent_divert.status_code == 403, f"Expected 403 Forbidden, got {agent_divert.status_code}: {agent_divert.text}"
    assert "Admin" in agent_divert.text or "administrator" in agent_divert.text.lower()

    # 7. Admin attempts to divert / reassign this accepted lead -> MUST SUCCEED with 200 OK!
    admin_divert = client.patch(
        f"/api/v1/leads/{lead_id}",
        json={"assigned_to": other_member_id},
        cookies={"timely_session": admin_token},
        headers=headers
    )
    print("Admin divert attempt status:", admin_divert.status_code)
    assert admin_divert.status_code == 200, admin_divert.text
    assert admin_divert.json()["assigned_to"] == other_member_id
    print("Lead successfully diverted by Admin to:", other_member_id)

if __name__ == "__main__":
    test_lead_acceptance_and_diversion()
    print("ALL TESTS PASSED SUCCESSFULLY!")
