"""add_lead_acceptance_and_user_auth_fields

Revision ID: a1b2c3d4e5f6
Revises: 9f3ca8ed0718
Create Date: 2026-10-16 20:05:00.000000

"""
import secrets as _sec
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.security import hash_password

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9f3ca8ed0718'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Reconcile 'leads' table
    existing_lead_cols = {c['name'] for c in inspector.get_columns('leads')}
    existing_lead_indexes = {i['name'] for i in inspector.get_indexes('leads')}

    with op.batch_alter_table('leads', schema=None) as batch_op:
        if 'accepted_by' not in existing_lead_cols:
            batch_op.add_column(sa.Column('accepted_by', sa.String(length=36), nullable=True))
        if 'accepted_at' not in existing_lead_cols:
            batch_op.add_column(sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True))
        if 'ix_leads_accepted_by' not in existing_lead_indexes:
            batch_op.create_index(batch_op.f('ix_leads_accepted_by'), ['accepted_by'], unique=False)

    # 2. Reconcile 'users' table
    existing_user_cols = {c['name'] for c in inspector.get_columns('users')}
    existing_user_indexes = {i['name'] for i in inspector.get_indexes('users')}

    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'auth_provider' not in existing_user_cols:
            batch_op.add_column(sa.Column('auth_provider', sa.String(length=40), nullable=True))
        if 'auth_subject' not in existing_user_cols:
            batch_op.add_column(sa.Column('auth_subject', sa.String(length=160), nullable=True))
        if 'token_version' not in existing_user_cols:
            batch_op.add_column(sa.Column('token_version', sa.Integer(), server_default='1', nullable=False))
        if 'sessions_revoked_at' not in existing_user_cols:
            batch_op.add_column(sa.Column('sessions_revoked_at', sa.DateTime(timezone=True), nullable=True))
        if 'ix_users_auth_subject' not in existing_user_indexes:
            batch_op.create_index(batch_op.f('ix_users_auth_subject'), ['auth_subject'], unique=False)

    # 3. Reconcile 'auth_sessions' table
    existing_session_cols = {c['name'] for c in inspector.get_columns('auth_sessions')}

    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        if 'created_at' not in existing_session_cols:
            batch_op.add_column(sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
        if 'token_version' not in existing_session_cols:
            batch_op.add_column(sa.Column('token_version', sa.Integer(), server_default='1', nullable=False))

    # 4. Data Migration (R4): Backfill existing users and invalidate legacy local password hashes
    users_table = sa.table(
        'users',
        sa.column('id', sa.String),
        sa.column('email', sa.String),
        sa.column('password_hash', sa.String),
        sa.column('auth_provider', sa.String),
        sa.column('token_version', sa.Integer)
    )

    sessions_table = sa.table(
        'auth_sessions',
        sa.column('user_id', sa.String)
    )

    users = bind.execute(sa.select(users_table.c.id, users_table.c.email, users_table.c.auth_provider)).fetchall()
    for u_id, u_email, u_prov in users:
        email_clean = (u_email or '').lower().strip()
        if u_prov is None:
            if email_clean == 'admin@timelyhealth.in':
                bind.execute(
                    sa.update(users_table)
                    .where(users_table.c.id == u_id)
                    .values(auth_provider='local', token_version=1)
                )
            else:
                # Federated accounts: tag as attendance, overwrite cached local hash with random secret, delete old sessions
                rand_hash = hash_password(_sec.token_urlsafe(32))
                bind.execute(
                    sa.update(users_table)
                    .where(users_table.c.id == u_id)
                    .values(auth_provider='attendance', password_hash=rand_hash, token_version=1)
                )
                bind.execute(
                    sa.delete(sessions_table).where(sessions_table.c.user_id == u_id)
                )


def downgrade():
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.drop_column('token_version')
        batch_op.drop_column('created_at')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_auth_subject'))
        batch_op.drop_column('sessions_revoked_at')
        batch_op.drop_column('token_version')
        batch_op.drop_column('auth_subject')
        batch_op.drop_column('auth_provider')

    with op.batch_alter_table('leads', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_leads_accepted_by'))
        batch_op.drop_column('accepted_at')
        batch_op.drop_column('accepted_by')
