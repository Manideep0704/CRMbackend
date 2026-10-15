from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now, uid


class Organization(Base):
    __tablename__ = 'organizations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='active')
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Membership(Base):
    __tablename__ = 'organization_memberships'
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default='agent')
    status: Mapped[str] = mapped_column(String(20), default='active')
    __table_args__ = (CheckConstraint("role IN ('admin','agent','viewer')"),)


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(['organization_id', 'user_id'], ['organization_memberships.organization_id', 'organization_memberships.user_id']),)


class Website(Base):
    __tablename__ = 'websites'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    name: Mapped[str] = mapped_column(String(160))
    domain: Mapped[str] = mapped_column(String(253))
    status: Mapped[str] = mapped_column(String(20), default='active')
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('organization_id', 'id'), UniqueConstraint('organization_id', 'domain'))


class WebsiteKey(Base):
    __tablename__ = 'website_api_keys'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    website_id: Mapped[str] = mapped_column(String(36))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (ForeignKeyConstraint(['organization_id', 'website_id'], ['websites.organization_id', 'websites.id']),)


class Lead(Base):
    __tablename__ = 'leads'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    website_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32), default='', index=True)
    email: Mapped[str] = mapped_column(String(254), default='', index=True)
    company_name: Mapped[str] = mapped_column(String(160), default='')
    inquiry_type: Mapped[str] = mapped_column(String(160), default='')
    source: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default='new', index=True)
    priority: Mapped[str] = mapped_column(String(20), default='medium')
    tags: Mapped[list] = mapped_column(JSON, default=list)
    attribution: Mapped[dict] = mapped_column(JSON, default=dict)
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    external_submission_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    next_follow_up_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    deleted_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('organization_id', 'id'),
        UniqueConstraint('website_id', 'external_submission_id'),
        ForeignKeyConstraint(['organization_id', 'website_id'], ['websites.organization_id', 'websites.id']),
        ForeignKeyConstraint(['organization_id', 'assigned_to'], ['organization_memberships.organization_id', 'organization_memberships.user_id']),
        ForeignKeyConstraint(['organization_id', 'duplicate_of'], ['leads.organization_id', 'leads.id']),
        CheckConstraint("status IN ('new','contacted','qualified','converted','lost')"),
        CheckConstraint("priority IN ('low','medium','high')"),
        Index('ix_leads_tenant_created', 'organization_id', 'created_at'),
    )


class Activity(Base):
    __tablename__ = 'lead_activities'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    type: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (ForeignKeyConstraint(['organization_id', 'lead_id'], ['leads.organization_id', 'leads.id']),)


class FollowUp(Base):
    __tablename__ = 'follow_up_tasks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    lead_id: Mapped[str] = mapped_column(String(36))
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    due_at: Mapped[object] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default='open')
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(['organization_id', 'lead_id'], ['leads.organization_id', 'leads.id']),
        ForeignKeyConstraint(['organization_id', 'assigned_to'], ['organization_memberships.organization_id', 'organization_memberships.user_id']),
    )


class ImportJob(Base):
    __tablename__ = 'import_jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    filename: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(20), default='preview')
    rows: Mapped[list] = mapped_column(JSON, default=list)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('organization_id', 'id'),)


class ImportErrorRow(Base):
    __tablename__ = 'import_row_errors'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    import_job_id: Mapped[str] = mapped_column(String(36))
    row_number: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(500))
    __table_args__ = (ForeignKeyConstraint(['organization_id', 'import_job_id'], ['import_jobs.organization_id', 'import_jobs.id']),)


class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey('organizations.id'), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
