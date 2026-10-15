import sys
import os
from uuid import uuid4
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from app.db import Base, get_db
from app.main import app
from app.models import Membership, Organization, User
from app.security import hash_password, limiter


@pytest.fixture()
def setup(tmp_path):
    postgres_url = os.getenv('TEST_DATABASE_URL')
    bootstrap, schema = None, None
    if postgres_url:
        if not postgres_url.startswith('postgresql'):
            raise ValueError('TEST_DATABASE_URL must target a disposable PostgreSQL test database')
        schema = 'test_' + uuid4().hex
        bootstrap = create_engine(postgres_url)
        with bootstrap.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA {schema}'))
        engine = create_engine(postgres_url, connect_args={'options': f'-csearch_path={schema}'})
    else:
        engine = create_engine(f'sqlite:///{tmp_path / "test.db"}', connect_args={'check_same_thread': False})
        @event.listens_for(engine, 'connect')
        def fk(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        a, b = Organization(name='Timely', slug='timely-health'), Organization(name='Other', slug='other')
        db.add_all([a, b]); db.flush()
        ids = {'a': a.id, 'b': b.id}
        for email, role, org in [('admin@test.in', 'admin', a), ('viewer@test.in', 'viewer', a), ('agent@test.in', 'agent', a), ('other@test.in', 'admin', b)]:
            user = User(email=email, name=email.split('@')[0], password_hash=hash_password('a-long-test-password'))
            db.add(user); db.flush()
            db.add(Membership(organization_id=org.id, user_id=user.id, role=role))
            ids[email] = user.id
        db.commit()
    def override():
        with factory() as db:
            try:
                yield db
            except Exception:
                db.rollback()
                raise
    app.dependency_overrides[get_db] = override
    limiter.buckets.clear()
    yield factory, ids
    app.dependency_overrides.clear()
    engine.dispose()
    if bootstrap:
        with bootstrap.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        bootstrap.dispose()


@pytest.fixture()
def clients(setup):
    result = {}
    for kind, email, slug in [('admin', 'admin@test.in', 'timely-health'), ('viewer', 'viewer@test.in', 'timely-health'), ('agent', 'agent@test.in', 'timely-health'), ('other', 'other@test.in', 'other')]:
        client = TestClient(app, headers={'X-Timely-Request': '1'})
        response = client.post('/api/v1/auth/login', json={'email': email, 'password': 'a-long-test-password', 'organization_slug': slug})
        assert response.status_code == 200, response.text
        result[kind] = client
    yield result
    for client in result.values():
        client.close()
