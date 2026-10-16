"""add_lead_acceptance_and_user_auth_fields

Revision ID: a1b2c3d4e5f6
Revises: 9f3ca8ed0718
Create Date: 2026-10-16 20:05:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '9f3ca8ed0718'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('leads', schema=None) as batch_op:
        batch_op.add_column(sa.Column('accepted_by', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_leads_accepted_by'), ['accepted_by'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auth_provider', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('auth_subject', sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column('token_version', sa.Integer(), server_default='1', nullable=False))
        batch_op.add_column(sa.Column('sessions_revoked_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_auth_subject'), ['auth_subject'], unique=False)

    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
        batch_op.add_column(sa.Column('token_version', sa.Integer(), server_default='1', nullable=False))


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
