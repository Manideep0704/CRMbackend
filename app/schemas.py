import re
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal['new', 'contacted', 'qualified', 'converted', 'lost']
Priority = Literal['low', 'medium', 'high']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class LeadInput(StrictModel):
    full_name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default='', max_length=40)
    email: str = Field(default='', max_length=254)
    company_name: str = Field(default='', max_length=160)
    inquiry_type: str = Field(default='', max_length=160)
    status: Status = 'new'
    priority: Priority = 'medium'
    website_id: str | None = None
    assigned_to: str | None = None
    next_follow_up_at: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('phone')
    @classmethod
    def normalize_phone(cls, value):
        if not value:
            return ''
        if re.search(r'[^\d\s+().-]', value):
            raise ValueError('Phone contains invalid characters')
        digits = re.sub(r'\D', '', value)
        if not 7 <= len(digits) <= 15:
            raise ValueError('Phone must contain 7 to 15 digits')
        if len(digits) == 12 and digits.startswith('91'):
            return digits[2:]
        if len(digits) == 11 and digits.startswith('0'):
            return digits[1:]
        if len(digits) == 10:
            return digits
        return ('+' if value.startswith('+') else '') + digits

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value):
        value = value.lower()
        if value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Email address is invalid')
        return value

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, value):
        if any(not t.strip() or len(t) > 50 for t in value):
            raise ValueError('Tags must contain 1 to 50 characters')
        return list(dict.fromkeys(t.strip() for t in value))

    @field_validator('next_follow_up_at')
    @classmethod
    def require_timezone(cls, value):
        if value and value.tzinfo is None:
            raise ValueError('Follow-up time must include a timezone')
        return value.astimezone(timezone.utc) if value else None


class Login(StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)
    organization_slug: str = Field(min_length=1, max_length=100)


class WebsiteInput(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    domain: str = Field(min_length=1, max_length=253)
    status: Literal['active', 'paused'] = 'active'
    field_mapping: dict[str, str] = Field(default_factory=dict, max_length=30)
    assigned_to: str | None = None

    @field_validator('domain')
    @classmethod
    def domain_only(cls, value):
        value = value.lower().strip().rstrip('.')
        if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', value):
            raise ValueError('Enter a domain without https:// or a path')
        return value

    @field_validator('field_mapping')
    @classmethod
    def safe_mapping(cls, value):
        allowed = {'full_name', 'phone', 'email', 'company_name', 'inquiry_type'}
        if any(not k or len(k) > 100 or v not in allowed for k, v in value.items()) or len(set(value.values())) != len(value):
            raise ValueError('Map unique form headings to supported contact fields')
        return value


class NoteInput(StrictModel):
    description: str = Field(min_length=1, max_length=4000)


class FollowUpInput(StrictModel):
    lead_id: str
    title: str = Field(min_length=1, max_length=240)
    due_at: datetime
    assigned_to: str | None = None

    @field_validator('due_at')
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError('Due time must include a timezone')
        return value.astimezone(timezone.utc)


class FollowUpUpdate(StrictModel):
    status: Literal['open', 'completed']


class CommitInput(StrictModel):
    import_job_id: str


class MembershipInput(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    role: Literal['admin', 'agent', 'viewer'] = 'agent'

    @field_validator('email')
    @classmethod
    def valid_email(cls, value):
        return LeadInput.normalize_email(value)


class MergeInput(StrictModel):
    target_id: str
