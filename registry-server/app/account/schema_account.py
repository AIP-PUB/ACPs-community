import uuid
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.utils.utils import BEIJING_TIMEZONE, utc_to_beijing


class RoleBase(BaseModel):
    name: str
    description: str | None = None


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class RoleResponse(RoleBase):
    id: uuid.UUID

    model_config = ConfigDict(from_attributes=True)


class UserBase(BaseModel):
    username: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    name: str | None = None
    avatar: str | None = None
    org_name: str | None = None
    org_code: str | None = None
    org_address: str | None = None

    @model_validator(mode="before")
    @classmethod
    def check_username_or_phone(cls, data: Any) -> Any:
        # 同时兼容字典式访问和对象属性访问
        if isinstance(data, dict):
            username = data.get("username")
            phone = data.get("phone")
        else:
            # 兼容 ORM 对象
            username = getattr(data, "username", None)
            phone = getattr(data, "phone", None)

        if username is None and phone is None:
            raise ValueError("Either username or phone must be provided")
        return data


class UserCreate(UserBase):
    password: str | None = None
    roles: list[str]


class UserUpdate(BaseModel):
    name: str | None = None
    avatar: str | None = None
    phone: str | None = None
    email: EmailStr | None = None
    email_code: str | None = None
    org_name: str | None = None
    org_code: str | None = None
    org_address: str | None = None


class UserUpdateCode(UserUpdate):
    code: str | None = None


class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str


class UpdatePasswordRequest(BaseModel):
    email: str
    code: str
    password: str


class PhoneUpdate(BaseModel):
    new_phone: str
    verify_code: str


class UserRoleUpdate(BaseModel):
    role_names: list[str]


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminPasswordReset(BaseModel):
    new_password: str


class AdminResetOtherUserPassword(BaseModel):
    current_password: str


class AicProviderCodeUpdate(BaseModel):
    aic_provider_code: str = Field(min_length=1, max_length=6)


class UserResponse(UserBase):
    id: uuid.UUID
    is_active: bool
    roles: list[str]  # List of role names as strings
    aic_provider_code: str | None = None
    created_at: datetime
    updated_at: datetime
    token_expires_at: datetime | None = None

    # 添加datetime字段的验证器，确保返回时带有北京时区信息并采用ISO 8601格式
    @field_validator("created_at", "updated_at", "token_expires_at", mode="before")
    @classmethod
    def convert_datetime_to_beijing(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            # 将UTC时间转换为北京时间，并确保带有时区信息
            beijing_time = utc_to_beijing(v)
            # 确保时间以ISO 8601格式返回带时区信息
            if beijing_time.tzinfo is not None:
                return beijing_time
            # 如果没有时区信息，添加北京时区信息
            return beijing_time.replace(tzinfo=BEIJING_TIMEZONE)
        return v

    model_config = ConfigDict(from_attributes=True)

    @field_validator("roles", mode="before")
    @classmethod
    def extract_role_names(cls, v: Any) -> Any:
        if v and isinstance(v, list) and not isinstance(v[0], str):
            return [role.name for role in v]
        return v


class ExternalPrincipalSummary(BaseModel):
    provider: str
    issuer: str
    principal_id: str
    username: str | None = None
    email: EmailStr | None = None


class CurrentUserResponse(UserResponse):
    external_principal: ExternalPrincipalSummary | None = None


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    page: int | None = None
    page_num: int | None = Field(default=None, deprecated=True)
    page_size: int | None = None

    @model_validator(mode="after")
    def sync_page_fields(self) -> UserListResponse:
        resolved_page = self.page if self.page is not None else self.page_num
        self.page = resolved_page
        self.page_num = resolved_page
        return self


class MessageResponse(BaseModel):
    message: str


class SuccessMessageResponse(BaseModel):
    success: bool
    message: str


class BatchDeleteUsersFailure(BaseModel):
    id: str
    reason: str


class BatchDeleteUsersResponse(BaseModel):
    success: list[str]
    failed: list[BatchDeleteUsersFailure]
