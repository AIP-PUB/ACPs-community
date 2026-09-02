from enum import StrEnum
from typing import Any

from fastapi import status

from app.core.base_exception import AppError


class VerificationErrorCode(StrEnum):
    """审核模块错误码。"""

    IDENTITY_ALREADY_VERIFIED = "IDENTITY_ALREADY_VERIFIED"
    IDENTITY_PENDING = "IDENTITY_PENDING"
    ORG_ALREADY_VERIFIED = "ORG_ALREADY_VERIFIED"
    ORG_PENDING = "ORG_PENDING"
    IDENTITY_NOT_VERIFIED = "IDENTITY_NOT_VERIFIED"
    VERIFICATION_NOT_FOUND = "VERIFICATION_NOT_FOUND"
    VERIFICATION_ALREADY_DECIDED = "VERIFICATION_ALREADY_DECIDED"
    VERIFICATION_REJECT_REMARK_REQUIRED = "VERIFICATION_REJECT_REMARK_REQUIRED"
    ORG_REQUIRES_IDENTITY_APPROVED = "ORG_REQUIRES_IDENTITY_APPROVED"
    VERIFICATION_CURRENT_RECORD_CONFLICT = "VERIFICATION_CURRENT_RECORD_CONFLICT"
    VERIFICATION_STALE_RECORD = "VERIFICATION_STALE_RECORD"
    VERIFICATION_DECRYPT_FAILED = "VERIFICATION_DECRYPT_FAILED"


class VerificationError(AppError):
    """审核相关异常的基类。"""

    def __init__(
        self,
        *,
        code: VerificationErrorCode,
        title: str,
        detail: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        input_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            code=code,
            title=title,
            detail=detail,
            type_=f"urn:acps:error:verification:{code.lower()}",
            extensions={
                "error_group": "verification",
                "input_params": input_params or {},
            },
        )


class IdentityAlreadyVerifiedError(VerificationError):
    """身份审核已经成功时抛出的异常。"""

    def __init__(self, *, user_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.IDENTITY_ALREADY_VERIFIED,
            title="Identity already verified",
            detail="Identity is already verified",
            status_code=status.HTTP_409_CONFLICT,
            input_params={"user_id": user_id},
        )


class IdentityVerificationPendingError(VerificationError):
    """身份审核已经处于待处理状态时抛出的异常。"""

    def __init__(self, *, user_id: str, verification_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.IDENTITY_PENDING,
            title="Identity verification pending",
            detail="Identity verification is already pending",
            status_code=status.HTTP_409_CONFLICT,
            input_params={"user_id": user_id, "verification_id": verification_id},
        )


class IdentityVerificationRequiredError(VerificationError):
    """在身份审核完成前请求组织审核时抛出的异常。"""

    def __init__(self, *, user_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.IDENTITY_NOT_VERIFIED,
            title="Identity verification required",
            detail="Identity verification is required before organization verification",
            status_code=status.HTTP_403_FORBIDDEN,
            input_params={"user_id": user_id},
        )


class OrganizationAlreadyVerifiedError(VerificationError):
    """组织审核已经成功时抛出的异常。"""

    def __init__(self, *, user_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.ORG_ALREADY_VERIFIED,
            title="Organization already verified",
            detail="Organization is already verified",
            status_code=status.HTTP_409_CONFLICT,
            input_params={"user_id": user_id},
        )


class OrganizationVerificationPendingError(VerificationError):
    """组织审核已经处于待处理状态时抛出的异常。"""

    def __init__(self, *, user_id: str, verification_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.ORG_PENDING,
            title="Organization verification pending",
            detail="Organization verification is already pending",
            status_code=status.HTTP_409_CONFLICT,
            input_params={"user_id": user_id, "verification_id": verification_id},
        )


class VerificationRecordNotFoundError(VerificationError):
    """认证记录不存在或已删除时抛出的异常。"""

    def __init__(self, *, verification_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_NOT_FOUND,
            title="Verification not found",
            detail="Verification record not found",
            status_code=status.HTTP_404_NOT_FOUND,
            input_params={"verification_id": verification_id},
        )


class VerificationAlreadyDecidedError(VerificationError):
    """认证记录已经完成审批时抛出的异常。"""

    def __init__(self, *, verification_id: str, status_value: str) -> None:
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_ALREADY_DECIDED,
            title="Verification already decided",
            detail="Verification record is no longer pending",
            status_code=status.HTTP_409_CONFLICT,
            input_params={"verification_id": verification_id, "status": status_value},
        )


class VerificationRejectRemarkRequiredError(VerificationError):
    """驳回认证时未提供原因时抛出的异常。"""

    def __init__(self, *, verification_id: str | None = None) -> None:
        input_params: dict[str, str] = {}
        if verification_id is not None:
            input_params["verification_id"] = verification_id
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_REJECT_REMARK_REQUIRED,
            title="Verification reject remark required",
            detail="A non-empty remark is required when rejecting verification",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            input_params=input_params,
        )


class OrgRequiresIdentityApprovedError(VerificationError):
    """组织认证批准前，用户没有有效个人实名时抛出的异常。"""

    def __init__(self, *, user_id: str, current_identity_id: str | None = None) -> None:
        input_params = {"user_id": user_id}
        if current_identity_id is not None:
            input_params["current_identity_id"] = current_identity_id
        super().__init__(
            code=VerificationErrorCode.ORG_REQUIRES_IDENTITY_APPROVED,
            title="Organization approval requires identity approved",
            detail="Approved identity verification is required before organization approval",
            status_code=status.HTTP_409_CONFLICT,
            input_params=input_params,
        )


class VerificationCurrentRecordConflictError(VerificationError):
    """用户已绑定其他当前认证记录时抛出的异常。"""

    def __init__(
        self,
        *,
        user_id: str,
        current_verification_id: str,
        attempted_verification_id: str,
    ) -> None:
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_CURRENT_RECORD_CONFLICT,
            title="Verification current record conflict",
            detail="User is already linked to another approved verification record",
            status_code=status.HTTP_409_CONFLICT,
            input_params={
                "user_id": user_id,
                "current_verification_id": current_verification_id,
                "attempted_verification_id": attempted_verification_id,
            },
        )


class VerificationStaleRecordError(VerificationError):
    """决策目标不是该用户最新认证记录时抛出的异常。"""

    def __init__(self, *, verification_id: str, latest_verification_id: str) -> None:
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_STALE_RECORD,
            title="Verification stale record",
            detail="Verification record is not the latest active record for this user",
            status_code=status.HTTP_409_CONFLICT,
            input_params={
                "verification_id": verification_id,
                "latest_verification_id": latest_verification_id,
            },
        )


class VerificationDecryptFailedError(VerificationError):
    """解密认证展示字段失败时抛出的异常。"""

    def __init__(self, *, verification_id: str, field_name: str) -> None:
        super().__init__(
            code=VerificationErrorCode.VERIFICATION_DECRYPT_FAILED,
            title="Verification decrypt failed",
            detail="Failed to decrypt verification field",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            input_params={"verification_id": verification_id, "field_name": field_name},
        )
