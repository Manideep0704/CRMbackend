from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_attendance_login():
    # 1. Test invalid password
    res_bad = client.post(
        "/api/v1/auth/login",
        json={"email": "saidulureddy@gmail.com", "password": "wrong_password_123", "organization_slug": "timely-health"},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    print("Invalid login response status:", res_bad.status_code)
    assert res_bad.status_code == 401

    # 2. Test valid admin login from attendanceDB
    res_admin = client.post(
        "/api/v1/auth/login",
        json={"email": "saidulureddy@gmail.com", "password": "reddy123", "organization_slug": "timely-health"},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    print("Admin login response status:", res_admin.status_code)
    print("Admin login response body:", res_admin.json())
    assert res_admin.status_code == 200
    assert res_admin.json()["role"] == "admin"

    # 3. Test valid employee login from attendanceDB
    res_emp = client.post(
        "/api/v1/auth/login",
        json={"email": "nareshyadav5313@gmail.com", "password": "naresh@123", "organization_slug": "timely-health"},
        headers={"X-Timely-Request": "1", "origin": "http://localhost:3000"}
    )
    print("Employee login response status:", res_emp.status_code)
    print("Employee login response body:", res_emp.json())
    assert res_emp.status_code == 200

    print("ALL ATTENDANCE DB AUTH TESTS PASSED!")

if __name__ == "__main__":
    test_attendance_login()
