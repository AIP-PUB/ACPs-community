import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.verification.model import (
    IdentityDocumentType,
    VerificationMethod,
    VerificationStatus,
)


class IdentityVerificationRequest(BaseModel):
    id_type: IdentityDocumentType = IdentityDocumentType.CN_ID_CARD
    id_number: str = Field(min_length=1, max_length=128)
    real_name: str = Field(min_length=1, max_length=128)

    @field_validator("id_number", "real_name", mode="before")
    @classmethod
    def normalize_required_strings(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        if not normalized:
            raise ValueError("Field cannot be blank")
        return normalized


class IdentityVerificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    id_type: IdentityDocumentType
    method: VerificationMethod
    provider: str | None = None
    status: VerificationStatus
    decided_at: datetime | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VerificationDecisionRequest(BaseModel):
    remark: str | None = Field(default=None, max_length=2000)

    @field_validator("remark", mode="before")
    @classmethod
    def normalize_remark(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        return normalized or None


class VerificationRejectRequest(BaseModel):
    remark: str = Field(min_length=1, max_length=2000)

    @field_validator("remark", mode="before")
    @classmethod
    def normalize_required_remark(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        if not normalized:
            raise ValueError("Field cannot be blank")
        return normalized


class AdminIdentityVerificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    id_type: IdentityDocumentType
    real_name: str
    method: VerificationMethod
    provider: str | None = None
    provider_request_id: str | None = None
    reviewer_id: uuid.UUID | None = None
    status: VerificationStatus
    decided_at: datetime | None = None
    remark: str | None = None
    attachment_urls: list[dict[str, str]] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrgVerificationRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=255)
    usci: str | None = Field(default=None, max_length=18)
    org_registration_number: str | None = Field(default=None, max_length=255)
    legal_rep_name: str | None = Field(default=None, max_length=128)
    legal_rep_id_number: str | None = Field(default=None, max_length=128)

    @field_validator("org_name", mode="before")
    @classmethod
    def normalize_org_name(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        if not normalized:
            raise ValueError("Field cannot be blank")
        return normalized

    @field_validator("usci", "org_registration_number", "legal_rep_name", "legal_rep_id_number", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip()
        return normalized or None


class OrgVerificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    org_name: str
    usci: str | None = None
    org_registration_number: str | None = None
    method: VerificationMethod
    provider: str | None = None
    status: VerificationStatus
    decided_at: datetime | None = None
    remark: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminOrgVerificationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    org_name: str
    usci: str | None = None
    org_registration_number: str | None = None
    legal_rep_name: str | None = None
    method: VerificationMethod
    provider: str | None = None
    provider_request_id: str | None = None
    reviewer_id: uuid.UUID | None = None
    status: VerificationStatus
    decided_at: datetime | None = None
    remark: str | None = None
    attachment_urls: list[dict[str, str]] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
