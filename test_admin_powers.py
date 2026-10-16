from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_admin_powers():
    # 1. Login as Admin (Saidulu Reddy)
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": "saidulureddy@gmail.com", "password": "reddy123", "organization_slug": "timely-health"},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.cookies.get("timely_session")
    assert admin_token is not None

    # 2. Login as Agent (Naresh Kumar)
    agent_login = client.post(
        "/api/v1/auth/login",
        json={"email": "nareshyadav5313@gmail.com", "password": "naresh@123", "organization_slug": "timely-health"},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    assert agent_login.status_code == 200
    agent_token = agent_login.cookies.get("timely_session")
    assert agent_token is not None

    # 3. Test Export Leads as Admin -> Must succeed with 200 and CSV content
    admin_export = client.get(
        "/api/v1/leads/export",
        cookies={"timely_session": admin_token},
        headers={"origin": "http://localhost:3000"}
    )
    print("Admin export status:", admin_export.status_code)
    assert admin_export.status_code == 200
    assert "Full Name" in admin_export.text
    print("Admin export CSV snippet:\n", admin_export.text[:200])

    # 4. Test Export Leads as Agent -> Must be REJECTED with 403 Forbidden
    agent_export = client.get(
        "/api/v1/leads/export",
        cookies={"timely_session": agent_token},
        headers={"origin": "http://localhost:3000"}
    )
    print("Agent export status:", agent_export.status_code)
    assert agent_export.status_code == 403
    print("Agent export correctly blocked with 403:", agent_export.json())

    # 5. Create a test lead to test deletion
    create_res = client.post(
        "/api/v1/leads",
        json={"full_name": "Test Delete Lead", "phone": "9998887776", "inquiry_type": "Testing"},
        cookies={"timely_session": admin_token},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    assert create_res.status_code == 201
    lead_id = create_res.json()["id"]

    # 6. Test Delete Lead as Agent -> Must be REJECTED with 403 Forbidden
    agent_delete = client.delete(
        f"/api/v1/leads/{lead_id}",
        cookies={"timely_session": agent_token},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    print("Agent delete status:", agent_delete.status_code)
    assert agent_delete.status_code == 403
    print("Agent delete correctly blocked with 403:", agent_delete.json())

    # 7. Test Delete Lead as Admin -> Must SUCCEED with 200
    admin_delete = client.delete(
        f"/api/v1/leads/{lead_id}",
        cookies={"timely_session": admin_token},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    print("Admin delete status:", admin_delete.status_code)
    assert admin_delete.status_code == 200
    print("Admin delete succeeded:", admin_delete.json())

    print("\nALL ADMIN PERMISSION TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_admin_powers()
