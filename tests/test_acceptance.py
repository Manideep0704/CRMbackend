import io
import json
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.db import now
from app.main import app
from app.models import Lead, Website, WebsiteKey
from app.security import limiter

ROOT = '/api/v1'


def create(client, **fields):
    response = client.post(ROOT + '/leads', json={'full_name': 'Example Person', **fields})
    assert response.status_code == 201, response.text
    return response.json()


def site(client, domain='digital.timelyhealth.in'):
    response = client.post(ROOT + '/websites', json={'name': 'Timely Digital', 'domain': domain})
    assert response.status_code == 201, response.text
    return response.json()


def test_manual_lead_normalization_search_update_delete(clients, setup):
    client = clients['admin']
    lead = create(client, phone='+91 (98765) 43210', email='Person@Example.com')
    assert lead['phone'] == '9876543210'
    assert lead['email'] == 'person@example.com'
    assert client.get(ROOT + '/leads?q=person@example').json()['total'] == 1
    response = client.patch(ROOT + '/leads/' + lead['id'], json={'status': 'qualified', 'priority': 'high'})
    assert response.status_code == 200, response.text
    with setup[0]() as db:
        assert db.get(Lead, lead['id']).status == 'qualified'
    assert client.delete(ROOT + '/leads/' + lead['id']).status_code == 200
    assert client.get(ROOT + '/leads').json()['total'] == 0
    assert client.get(ROOT + '/leads/' + lead['id']).status_code == 404


def test_tenant_isolation_every_resource(clients):
    a, b = clients['admin'], clients['other']
    lead = create(a)
    website = site(a)
    task = a.post(ROOT + '/follow-ups', json={'lead_id': lead['id'], 'title': 'Call', 'due_at': (now() + timedelta(days=1)).isoformat()}).json()
    preview = a.post(ROOT + '/imports/preview', files={'file': ('data.csv', b'Name\nTest Import\n', 'text/csv')}, data={'mapping': json.dumps({'full_name': 'Name'})}).json()
    for path in [f"/leads/{lead['id']}", f"/imports/{preview['id']}/errors"]:
        assert b.get(ROOT + path).status_code == 404
    assert b.patch(ROOT + f"/leads/{lead['id']}", json={'status': 'lost'}).status_code == 404
    assert b.delete(ROOT + f"/leads/{lead['id']}").status_code == 404
    assert b.post(ROOT + f"/leads/{lead['id']}/notes", json={'description': 'unauthorized'}).status_code == 404
    assert b.post(ROOT + f"/websites/{website['id']}/rotate-key").status_code == 404
    assert b.patch(ROOT + f"/follow-ups/{task['id']}", json={'status': 'completed'}).status_code == 404
    assert b.post(ROOT + '/imports/commit', json={'import_job_id': preview['id']}).status_code == 404
    for path in ['/leads', '/websites', '/follow-ups', '/imports', '/audit-logs']:
        data = b.get(ROOT + path).json()
        assert data == [] or data.get('total') == 0
    assert b.get(ROOT + '/dashboard/summary').json()['total'] == 0


def test_cross_tenant_assignment_and_database_constraints(clients, setup):
    website = site(clients['other'])
    assert clients['admin'].post(ROOT + '/leads', json={'full_name': 'Test', 'website_id': website['id']}).status_code == 404
    assert clients['admin'].post(ROOT + '/leads', json={'full_name': 'Test', 'assigned_to': setup[1]['other@test.in']}).status_code == 422
    factory, ids = setup
    with factory() as db:
        db.add(Lead(organization_id=ids['a'], website_id=website['id'], full_name='Forbidden', source='manual'))
        with pytest.raises(IntegrityError):
            db.commit()


def test_roles_sessions_csrf_and_invalid_login(clients):
    assert clients['viewer'].post(ROOT + '/leads', json={'full_name': 'Forbidden'}).status_code == 403
    assert clients['agent'].post(ROOT + '/websites', json={'name': 'No', 'domain': 'example.com'}).status_code == 403
    assert clients['viewer'].get(ROOT + '/leads').status_code == 200
    assert clients['viewer'].get(ROOT + '/audit-logs').status_code == 403
    anonymous = TestClient(app)
    assert anonymous.get(ROOT + '/leads').status_code == 401
    assert anonymous.post(ROOT + '/auth/login', json={'email': 'admin@test.in', 'password': 'a-long-test-password', 'organization_slug': 'timely-health'}).status_code == 403
    assert clients['admin'].post(ROOT + '/leads', json={'full_name': 'X'}, headers={'Origin': 'https://attacker.example'}).status_code == 403
    limiter.buckets.clear()
    assert clients['admin'].post(ROOT + '/auth/login', json={'email': 'admin@test.in', 'password': 'wrong', 'organization_slug': 'timely-health'}).status_code == 401
    assert clients['admin'].post(ROOT + '/auth/logout').status_code == 200
    assert clients['admin'].get(ROOT + '/leads').status_code == 401


def test_website_ingestion_retry_mapping_rotation_and_second_site(clients, setup):
    a = clients['admin']
    website = site(a)
    assert a.patch(ROOT + f"/websites/{website['id']}", json={'name': 'Timely', 'domain': website['domain'], 'field_mapping': {'your-name': 'full_name', 'your-email': 'email'}}).status_code == 200
    key = a.post(ROOT + f"/websites/{website['id']}/rotate-key").json()['key']
    public = TestClient(app)
    payload = {'your-name': 'Website Person', 'your-email': 'web@example.com', 'form_id': 'contact', 'utm_source': 'test'}
    headers = {'X-API-Key': key, 'Idempotency-Key': 'submission-1'}
    for _ in range(2):
        response = public.post(ROOT + '/public/leads', json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {'accepted': True, 'channel': 'primary_webhook'}
    leads = a.get(ROOT + '/leads').json()
    assert leads['total'] == 1
    assert leads['items'][0]['website_id'] == website['id']
    assert leads['items'][0]['attribution']['utm_source'] == 'test'
    assert public.post(ROOT + '/public/leads', json={**payload, 'organization_id': setup[1]['b']}, headers=headers).status_code == 422
    assert public.post(ROOT + '/public/leads', json=payload, headers={'X-API-Key': key}).status_code == 422
    with setup[0]() as db:
        assert db.scalar(select(WebsiteKey)).key_hash != key
    second = site(a, 'second.example.com')
    second_key = a.post(ROOT + f"/websites/{second['id']}/rotate-key").json()['key']
    assert public.post(ROOT + '/public/leads', json={'name': 'Second', 'external_submission_id': 'submission-1'}, headers={'X-API-Key': second_key}).status_code == 200
    a.post(ROOT + f"/websites/{website['id']}/rotate-key")
    assert public.post(ROOT + '/public/leads', json=payload, headers=headers).status_code == 401
    a.delete(ROOT + f"/websites/{second['id']}/key")
    assert public.post(ROOT + '/public/leads', json={'name': 'X'}, headers={'X-API-Key': second_key}).status_code == 401


def test_duplicate_review_and_controlled_merge(clients):
    client = clients['admin']
    first = create(client, email='same@example.com')
    second = create(client, email='same@example.com', company_name='Company')
    assert second['duplicate_of'] == first['id']
    response = client.post(ROOT + f"/leads/{second['id']}/merge", json={'target_id': first['id']})
    assert response.status_code == 200, response.text
    assert response.json()['company_name'] == 'Company'
    assert client.get(ROOT + '/leads').json()['total'] == 1
    assert client.get(ROOT + f"/leads/{first['id']}").json()['activities'][0]


def test_import_valid_invalid_duplicate_retry_report(clients):
    a = clients['admin']
    content = b'Name,Email,Phone\nAlice,alice@example.com,+919876543210\nAgain,ALICE@example.com,+919876543210\nBad,bad-email,abc\nNo Contact,,\n'
    response = a.post(ROOT + '/imports/preview', files={'file': ('leads.csv', content)}, data={'mapping': json.dumps({'full_name': 'Name', 'email': 'Email', 'phone': 'Phone'})})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job['counts'] == {'valid': 2, 'duplicate': 1, 'failed': 1}
    for _ in range(2):
        result = a.post(ROOT + '/imports/commit', json={'import_job_id': job['id']})
        assert result.status_code == 200, result.text
        assert result.json()['counts']['imported'] == 2
    assert a.get(ROOT + '/leads').json()['total'] == 2
    report = a.get(ROOT + f"/imports/{job['id']}/errors")
    assert 'duplicate' in report.text and 'failed' in report.text and 'bad-email' not in report.text
    repeated = a.post(ROOT + '/imports/preview', files={'file': ('leads.csv', content)}, data={'mapping': json.dumps({'full_name': 'Name', 'email': 'Email', 'phone': 'Phone'})}).json()
    assert repeated['counts']['duplicate'] == 2


def test_xlsx_mapping_and_formula_rejection(clients):
    book = Workbook(); sheet = book.active
    sheet.append(['Customer', 'Contact']); sheet.append(['Excel Person', 'excel@example.com'])
    out = io.BytesIO(); book.save(out)
    client = clients['admin']
    inspected = client.post(ROOT + '/imports/inspect', files={'file': ('leads.xlsx', out.getvalue())})
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()['headers'] == ['Customer', 'Contact']
    preview = client.post(ROOT + '/imports/preview', files={'file': ('leads.xlsx', out.getvalue())}, data={'mapping': json.dumps({'full_name': 'Customer', 'email': 'Contact'})}).json()
    assert client.post(ROOT + '/imports/commit', json={'import_job_id': preview['id']}).json()['counts']['imported'] == 1
    sheet.append(['=1+1', 'formula@example.com']); out = io.BytesIO(); book.save(out)
    assert client.post(ROOT + '/imports/inspect', files={'file': ('formula.xlsx', out.getvalue())}).status_code == 422
    assert client.post(ROOT + '/imports/inspect', files={'file': ('script.exe', b'bad')}).status_code == 422
    assert client.post(ROOT + '/imports/inspect', files={'file': ('big.csv', b'x' * (5 * 1024 * 1024 + 1))}).status_code == 413


def test_followup_persistence_completion_and_kpi_cohort(clients, setup):
    client = clients['admin']
    lead = create(client, next_follow_up_at=(now() - timedelta(hours=1)).isoformat())
    tasks = client.get(ROOT + '/follow-ups').json()
    assert len(tasks) == 1
    assert client.get(ROOT + '/dashboard/summary').json()['overdue'] == 1
    result = client.patch(ROOT + f"/follow-ups/{tasks[0]['id']}", json={'status': 'completed'})
    assert result.status_code == 200, result.text
    assert client.get(ROOT + f"/leads/{lead['id']}").json()['next_follow_up_at'] is None
    client.patch(ROOT + f"/leads/{lead['id']}", json={'status': 'converted'})
    summary = client.get(ROOT + '/dashboard/summary').json()
    assert summary['conversion_rate'] == 100 and summary['overdue'] == 0
    future = (now() + timedelta(days=1)).isoformat()
    assert client.get(ROOT + '/dashboard/summary', params={'date_from': future}).json()['total'] == 0
    with setup[0]() as db:
        assert db.get(Lead, lead['id']).status == 'converted'


def test_validation_and_spam_limits(clients):
    client = clients['admin']
    for payload in [{'full_name': ''}, {'full_name': 'X', 'email': 'invalid'}, {'full_name': 'X', 'phone': 'abc'}, {'full_name': 'X', 'organization_id': 'untrusted'}]:
        assert client.post(ROOT + '/leads', json=payload).status_code == 422
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as limited:
        for _ in range(3):
            limiter.check('bounded-test', 2, 60)
    assert limited.value.status_code == 429


def test_followup_date_can_be_edited_without_duplicate_tasks(clients):
    client = clients['admin']
    lead = create(client, next_follow_up_at=(now() + timedelta(days=1)).isoformat())
    assert client.patch(ROOT + f"/leads/{lead['id']}", json={'status': 'qualified'}).status_code == 200
    assert client.patch(ROOT + f"/leads/{lead['id']}", json={'next_follow_up_at': (now() + timedelta(days=2)).isoformat()}).status_code == 200
    assert len(client.get(ROOT + '/follow-ups').json()) == 1
    assert client.patch(ROOT + f"/leads/{lead['id']}", json={'next_follow_up_at': None}).status_code == 200
    assert client.get(ROOT + '/follow-ups').json() == []


def test_malformed_mapping_and_invalid_member_email_return_safe_errors(clients):
    client = clients['admin']
    for mapping in [{'full_name': []}, {'organization_id': 'Name'}, {}]:
        response = client.post(ROOT + '/imports/preview', files={'file': ('sample.csv', b'Name\nSample\n')}, data={'mapping': json.dumps(mapping)})
        assert response.status_code == 422, response.text
    response = client.post(ROOT + '/members', json={'name': 'Test', 'email': 'bad-email', 'password': 'long-enough-password', 'role': 'agent'})
    assert response.status_code == 422


def test_timezone_offsets_are_normalized_for_local_and_postgres_storage(clients):
    client = clients['admin']
    lead = create(client, next_follow_up_at='2026-10-20T10:00:00+05:30')
    saved = client.get(ROOT + f"/leads/{lead['id']}").json()
    assert saved['next_follow_up_at'] == '2026-10-20T04:30:00+00:00'
    assert client.get(ROOT + '/follow-ups').json()[0]['due_at'] == '2026-10-20T04:30:00+00:00'
    assert client.get(ROOT + '/leads', params={'date_from': '2026-10-20T10:00:00'}).status_code == 422
