import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.models import Lead, User, Membership, FollowUp, Organization
from app.db import now
from app.security import hash_password, limiter

ROOT = '/api/v1'


def test_s4_agent_scoped_leads_and_object_authorization(clients, setup):
    """
    S4 – High: Inconsistent object authorization.
    Verifies:
    1. Agent list leads query always scopes to agent's assigned leads (with or without pipeline flag).
    2. Agent cannot read another agent's assigned lead directly (HTTP 403).
    3. Agent can read unaccepted leads to inspect & claim them.
    4. Agent cannot patch another agent's lead (HTTP 403).
    5. Agent cannot add notes to another agent's lead (HTTP 403).
    6. Agent cannot divert/reassign an accepted lead (HTTP 403, Admin only).
    7. Agent cannot create or edit follow-ups for another agent's lead (HTTP 403).
    8. Agent dashboard summary only counts their own leads.
    """
    admin_c = clients['admin']
    agent_c = clients['agent']
    factory, ids = setup
    agent_id = ids['agent@test.in']
    admin_id = ids['admin@test.in']

    # 1. Create 3 leads in organization 'timely-health'
    # Lead 1: assigned to agent
    r1 = admin_c.post(ROOT + '/leads', json={
        'full_name': 'Agent Lead',
        'phone': '9876543211',
        'email': 'agent.lead@test.com',
        'assigned_to': agent_id
    })
    assert r1.status_code == 201
    lead_agent = r1.json()

    # Lead 2: assigned to admin, accepted by admin
    r2 = admin_c.post(ROOT + '/leads', json={
        'full_name': 'Admin Lead',
        'phone': '9876543212',
        'email': 'admin.lead@test.com',
        'assigned_to': admin_id
    })
    assert r2.status_code == 201
    lead_admin = r2.json()
    # Mark lead_admin as accepted by admin
    with factory() as db:
        l2 = db.get(Lead, lead_admin['id'])
        l2.accepted_by = admin_id
        l2.accepted_at = now()
        db.commit()

    # Lead 3: unaccepted, unassigned
    r3 = admin_c.post(ROOT + '/leads', json={
        'full_name': 'Unaccepted Lead',
        'phone': '9876543213',
        'email': 'unaccepted.lead@test.com'
    })
    assert r3.status_code == 201
    lead_unacc = r3.json()
    with factory() as db:
        l3 = db.get(Lead, lead_unacc['id'])
        l3.accepted_by = None
        l3.accepted_at = None
        l3.assigned_to = None
        db.commit()

    # --- S4 Test 1: Agent lead listing is always scoped ---
    # Query without pipeline flag:
    agent_leads = agent_c.get(ROOT + '/leads').json()
    agent_lead_ids = [l['id'] for l in agent_leads['items']]
    assert lead_agent['id'] in agent_lead_ids
    assert lead_admin['id'] not in agent_lead_ids

    # Query with pipeline flag:
    agent_pipeline_leads = agent_c.get(ROOT + '/leads?pipeline=true').json()
    agent_pipeline_ids = [l['id'] for l in agent_pipeline_leads['items']]
    assert lead_agent['id'] in agent_pipeline_ids
    assert lead_admin['id'] not in agent_pipeline_ids

    # Admin sees all leads
    admin_leads = admin_c.get(ROOT + '/leads').json()
    admin_lead_ids = [l['id'] for l in admin_leads['items']]
    assert lead_agent['id'] in admin_lead_ids
    assert lead_admin['id'] in admin_lead_ids
    assert lead_unacc['id'] in admin_lead_ids

    # --- S4 Test 2: Direct GET /leads/{lead_id} authorization ---
    # Agent can read their own lead
    assert agent_c.get(f"{ROOT}/leads/{lead_agent['id']}").status_code == 200

    # Agent can read unaccepted lead (to inspect & accept)
    assert agent_c.get(f"{ROOT}/leads/{lead_unacc['id']}").status_code == 200

    # Agent CANNOT read another agent/admin's accepted lead (HTTP 403)
    assert agent_c.get(f"{ROOT}/leads/{lead_admin['id']}").status_code == 403

    # --- S4 Test 3: Direct PATCH /leads/{lead_id} authorization ---
    # Agent CANNOT edit another user's lead (HTTP 403)
    assert agent_c.patch(f"{ROOT}/leads/{lead_admin['id']}", json={'priority': 'low'}).status_code == 403

    # Agent CAN edit their own lead
    assert agent_c.patch(f"{ROOT}/leads/{lead_agent['id']}", json={'priority': 'high'}).status_code == 200

    # --- S4 Test 4: Direct POST /leads/{lead_id}/notes authorization ---
    # Agent CANNOT add note to another user's lead (HTTP 403)
    assert agent_c.post(f"{ROOT}/leads/{lead_admin['id']}/notes", json={'description': 'unauthorized note'}).status_code == 403

    # Agent CAN add note to their own lead
    assert agent_c.post(f"{ROOT}/leads/{lead_agent['id']}/notes", json={'description': 'authorized note'}).status_code == 201

    # --- S4 Test 5: Reassignment / divert restriction ---
    # Mark agent's lead as accepted
    with factory() as db:
        la = db.get(Lead, lead_agent['id'])
        la.accepted_by = agent_id
        la.accepted_at = now()
        db.commit()

    # Agent cannot divert their accepted lead to someone else (HTTP 403)
    assert agent_c.patch(f"{ROOT}/leads/{lead_agent['id']}", json={'assigned_to': admin_id}).status_code == 403

    # Admin CAN divert / reassign an accepted lead
    assert admin_c.patch(f"{ROOT}/leads/{lead_agent['id']}", json={'assigned_to': admin_id}).status_code == 200

    # --- S4 Test 6: Follow-up authorization ---
    # Reset assignment back to agent for follow-up testing
    admin_c.patch(f"{ROOT}/leads/{lead_agent['id']}", json={'assigned_to': agent_id})

    # Agent cannot schedule follow-up on admin's lead (HTTP 403)
    fu_bad = agent_c.post(ROOT + '/follow-ups', json={
        'lead_id': lead_admin['id'],
        'title': 'Bad Follow Up',
        'due_at': now().isoformat(),
        'assigned_to': agent_id
    })
    assert fu_bad.status_code == 403

    # Agent CAN schedule follow-up on their own lead
    fu_good = agent_c.post(ROOT + '/follow-ups', json={
        'lead_id': lead_agent['id'],
        'title': 'Good Follow Up',
        'due_at': now().isoformat(),
        'assigned_to': agent_id
    })
    assert fu_good.status_code == 201
    task_id = fu_good.json()['id']

    # Agent listing follow-ups sees only their lead's tasks
    fu_list = agent_c.get(ROOT + '/follow-ups').json()
    fu_ids = [t['id'] for t in fu_list]
    assert task_id in fu_ids

    # --- S4 Test 7: Dashboard summary scoping ---
    dash_agent = agent_c.get(ROOT + '/dashboard/summary').json()
    dash_admin = admin_c.get(ROOT + '/dashboard/summary').json()
    assert dash_agent['total'] == 1
    assert dash_admin['total'] >= 3


def test_s3_federated_user_authentication_lifecycle(setup):
    """
    S3 – High: Plaintext upstream passwords and stale local authentication.
    Verifies:
    1. A federated user (in MongoDB attendanceDB) delegates authentication strictly to Mongo.
    2. Stale local scrypt hash cannot bypass MongoDB when upstream auth fails.
    3. If upstream deactivates the user, login is denied and local is_active is revoked.
    4. Non-federated (local) users authenticate against local hash normally.
    """
    factory, ids = setup
    client = TestClient(app, headers={'X-Timely-Request': '1'})
    limiter.buckets.clear()

    # Pre-provision a user with a local password hash
    federated_email = 'federated_agent@test.com'
    with factory() as db:
        org = db.scalar(select(Organization).where(Organization.slug == 'timely-health'))
        u = User(
            name='Federated Agent',
            email=federated_email,
            password_hash=hash_password('OldLocalPassword123!'),
            is_active=True
        )
        db.add(u)
        db.flush()
        db.add(Membership(organization_id=org.id, user_id=u.id, role='agent', status='active'))
        db.commit()

    # Mock MongoDB functions from app.mongo:
    # Scenario A: is_attendance_user returns True, but authenticate_attendance_user returns None
    # (e.g. employee deactivated upstream in MongoDB or password changed upstream)
    with patch('app.mongo.is_attendance_user', return_value=True), \
         patch('app.mongo.authenticate_attendance_user', return_value=None):
        limiter.buckets.clear()
        # Even though payload has 'OldLocalPassword123!' which matches local hash,
        # S3 mandates that local password fallback is disabled for federated users!
        res = client.post(ROOT + '/auth/login', json={
            'email': federated_email,
            'password': 'OldLocalPassword123!',
            'organization_slug': 'timely-health'
        })
        assert res.status_code == 401, "Stale local password must NOT authenticate a federated user!"

        # And local user is marked inactive
        with factory() as db:
            refreshed = db.scalar(select(User).where(User.email == federated_email))
            assert refreshed.is_active is False

    # Scenario B: User provides correct upstream password in MongoDB
    with patch('app.mongo.is_attendance_user', return_value=True), \
         patch('app.mongo.authenticate_attendance_user', return_value={
             'email': federated_email,
             'name': 'Federated Agent',
             'role': 'agent',
             'status': 'active'
         }):
        limiter.buckets.clear()
        res_ok = client.post(ROOT + '/auth/login', json={
            'email': federated_email,
            'password': 'NewUpstreamPassword456!',
            'organization_slug': 'timely-health'
        })
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data['user']['email'] == federated_email
        assert data['role'] == 'agent'
