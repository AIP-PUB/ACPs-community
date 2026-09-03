from pydantic import BaseModel, EmailStr


class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str | None = None
    expires_at: str | None = None  # ISO format timestamp when the token expires


class TokenPayload(BaseModel):
    sub: str | None = None


class VerifyCodeRequest(BaseModel):
    phone: str


class VerifyCodeResponse(BaseModel):
    message: str
    code: str  # In a real system, this would not be returned, but sent via SMS


class MessageResponse(BaseModel):
    message: str


class SuccessMessageResponse(BaseModel):
    success: bool
    message: str


class OidcLogoutResponse(SuccessMessageResponse):
    end_session_endpoint: str | None = None


class RegisterRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    phone: str | None = None
    verify_code: str | None = None
    email: EmailStr | None = None
    name: str | None = None
    org_name: str | None = None
    org_code: str | None = None
    org_address: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class PhoneLoginRequest(BaseModel):
    phone: str
    verify_code: str


class ResetPasswordRequest(BaseModel):
    phone: str
    verify_code: str
    new_password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str
