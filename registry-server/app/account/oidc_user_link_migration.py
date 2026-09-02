from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from acps_sdk.oidc import build_principal_id
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.account.model import User
from app.core.db_session import close_sync_engine, get_sync_session

USER_ID_COLUMN = cast("Any", User.id)
USER_USERNAME_COLUMN = cast("Any", User.username)
USER_EXTERNAL_PRINCIPAL_ID_COLUMN = cast("Any", User.external_principal_id)
USER_EXTERNAL_ISSUER_COLUMN = cast("Any", User.external_issuer)
USER_EXTERNAL_SUBJECT_COLUMN = cast("Any", User.external_subject)


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _require_text(raw: object, *, field_name: str) -> str:
    normalized = _normalize_optional_text(raw)
    if normalized is None:
        raise ValueError(f"mapping field {field_name} is required")
    return normalized


def _hash_text(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_line(stream: Any, message: str) -> None:
    stream.write(f"{message}\n")


@dataclass(frozen=True)
class OidcUserLinkMapping:
    issuer: str
    subject: str = field(repr=False)
    username: str | None = None
    user_id: UUID | None = None
    expected_email: str | None = None

    @classmethod
    def from_mapping(cls, raw: object) -> OidcUserLinkMapping:
        if not isinstance(raw, dict):
            raise ValueError("each mapping must be a JSON object")

        username = _normalize_optional_text(raw.get("username"))
        user_id_value = _normalize_optional_text(raw.get("user_id"))
        expected_email = _normalize_optional_text(raw.get("expected_email"))
        if username is None and user_id_value is None:
            raise ValueError("each mapping requires username or user_id; email-only matching is not supported")

        user_id = None
        if user_id_value is not None:
            try:
                user_id = UUID(user_id_value)
            except ValueError as exc:
                raise ValueError(f"mapping field user_id is not a valid UUID: {user_id_value}") from exc

        return cls(
            issuer=_require_text(raw.get("issuer"), field_name="issuer"),
            subject=_require_text(raw.get("subject"), field_name="subject"),
            username=username,
            user_id=user_id,
            expected_email=expected_email,
        )

    @property
    def locator(self) -> dict[str, str]:
        locator: dict[str, str] = {}
        if self.user_id is not None:
            locator["user_id"] = str(self.user_id)
        if self.username is not None:
            locator["username"] = self.username
        return locator

    @property
    def principal_id(self) -> str:
        return build_principal_id(issuer=self.issuer, subject=self.subject)

    @property
    def subject_hash(self) -> str:
        return hashlib.sha256(self.subject.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OidcUserLinkResult:
    locator: dict[str, str]
    status: str
    message: str
    blocking: bool
    principal_id: str
    subject_hash: str
    user_id: str | None = None
    username: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "locator": self.locator,
            "status": self.status,
            "message": self.message,
            "blocking": self.blocking,
            "principal_id": self.principal_id,
            "subject_hash": self.subject_hash,
        }
        for key, value in (
            ("user_id", self.user_id),
            ("username", self.username),
            ("before", self.before),
            ("after", self.after),
        ):
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class OidcUserLinkReport:
    dry_run: bool
    applied_count: int
    blocking_count: int
    results: tuple[OidcUserLinkResult, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "applied_count": self.applied_count,
            "blocking_count": self.blocking_count,
            "results": [result.to_json_dict() for result in self.results],
        }


@dataclass(frozen=True)
class _OidcUserLinkPlan:
    mapping: OidcUserLinkMapping
    user: User | None
    result: OidcUserLinkResult
    can_apply: bool


def load_link_mappings(mapping_file: Path) -> list[OidcUserLinkMapping]:
    try:
        payload = json.loads(mapping_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"mapping file does not exist: {mapping_file}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"mapping file is not valid JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise ValueError("mapping file must contain a JSON array")
    return [OidcUserLinkMapping.from_mapping(item) for item in payload]


def migrate_local_users_to_oidc(
    session: Session,
    mappings: list[OidcUserLinkMapping],
    *,
    dry_run: bool,
) -> OidcUserLinkReport:
    plans = _build_link_plans(session, mappings)
    blocking_count = sum(1 for plan in plans if plan.result.blocking)
    applied_count = 0
    results = [plan.result for plan in plans]

    if not dry_run and blocking_count == 0:
        applied_results: list[OidcUserLinkResult] = []
        for plan in plans:
            if not plan.can_apply or plan.user is None:
                applied_results.append(plan.result)
                continue

            user = plan.user
            user.external_issuer = plan.mapping.issuer
            user.external_subject = plan.mapping.subject
            user.external_principal_id = plan.mapping.principal_id
            session.add(user)
            applied_count += 1
            applied_results.append(
                replace(
                    plan.result,
                    status="linked",
                    message="linked local user to OIDC principal",
                    after=_project_user_state(user),
                )
            )
        session.flush()
        results = applied_results

    return OidcUserLinkReport(
        dry_run=dry_run,
        applied_count=applied_count,
        blocking_count=blocking_count,
        results=tuple(results),
    )


def _build_link_plans(session: Session, mappings: list[OidcUserLinkMapping]) -> list[_OidcUserLinkPlan]:
    plans: list[_OidcUserLinkPlan] = []
    seen_user_ids: set[str] = set()
    seen_principal_ids: set[str] = set()

    for mapping in mappings:
        user_from_id = _get_user_by_id(session, mapping.user_id) if mapping.user_id is not None else None
        user_from_username = _get_user_by_username(session, mapping.username) if mapping.username is not None else None

        if user_from_id is not None and user_from_username is not None and user_from_id.id != user_from_username.id:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=None,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="locator_conflict",
                        message="user_id and username resolve to different users",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                    ),
                )
            )
            continue

        user = user_from_id or user_from_username
        if user is None:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=None,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="missing_user",
                        message="no local user matched the explicit locator",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                    ),
                )
            )
            continue

        before = _project_user_state(user)
        user_id_value = str(user.id)
        if user_id_value in seen_user_ids:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="duplicate_target",
                        message="the mapping file references the same local user more than once",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        if mapping.principal_id in seen_principal_ids:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="duplicate_principal",
                        message="the mapping file references the same OIDC principal more than once",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        if mapping.expected_email is not None and user.email != mapping.expected_email:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="email_mismatch",
                        message="expected_email does not match the target local user; email auto-merge is disabled",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        if user.external_principal_id and user.external_principal_id != mapping.principal_id:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="user_already_linked",
                        message="target local user is already linked to a different OIDC principal",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        if user.external_issuer not in (None, mapping.issuer) or (
            user.external_subject is not None and _hash_text(user.external_subject) != mapping.subject_hash
        ):
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="user_link_metadata_conflict",
                        message="target local user already has different OIDC linkage metadata",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        existing_principal_user = _get_user_by_external_principal_id(session, mapping.principal_id)
        if existing_principal_user is not None and existing_principal_user.id != user.id:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="principal_conflict",
                        message="another user is already linked to this OIDC principal_id",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        existing_identity_user = _get_user_by_external_identity(session, mapping.issuer, mapping.subject)
        if existing_identity_user is not None and existing_identity_user.id != user.id:
            plans.append(
                _OidcUserLinkPlan(
                    mapping=mapping,
                    user=user,
                    can_apply=False,
                    result=OidcUserLinkResult(
                        locator=mapping.locator,
                        status="issuer_subject_conflict",
                        message="another user is already linked to this issuer/subject pair",
                        blocking=True,
                        principal_id=mapping.principal_id,
                        subject_hash=mapping.subject_hash,
                        user_id=user_id_value,
                        username=user.username,
                        before=before,
                    ),
                )
            )
            continue

        seen_user_ids.add(user_id_value)
        seen_principal_ids.add(mapping.principal_id)
        already_linked = (
            user.external_issuer == mapping.issuer
            and user.external_principal_id == mapping.principal_id
            and _hash_text(user.external_subject) == mapping.subject_hash
        )
        status = "already_linked" if already_linked else "would_link"
        message = (
            "local user is already linked to this OIDC principal"
            if already_linked
            else "local user can be linked to the OIDC principal"
        )
        plans.append(
            _OidcUserLinkPlan(
                mapping=mapping,
                user=user,
                can_apply=not already_linked,
                result=OidcUserLinkResult(
                    locator=mapping.locator,
                    status=status,
                    message=message,
                    blocking=False,
                    principal_id=mapping.principal_id,
                    subject_hash=mapping.subject_hash,
                    user_id=user_id_value,
                    username=user.username,
                    before=before,
                    after=_project_user_state(
                        user,
                        override_issuer=mapping.issuer,
                        override_subject_hash=mapping.subject_hash,
                        override_principal_id=mapping.principal_id,
                    ),
                ),
            )
        )

    return plans


def _project_user_state(
    user: User,
    *,
    override_issuer: str | None = None,
    override_subject_hash: str | None = None,
    override_principal_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "auth_provider": user.auth_provider,
        "external_issuer": override_issuer if override_issuer is not None else user.external_issuer,
        "external_principal_id": (
            override_principal_id if override_principal_id is not None else user.external_principal_id
        ),
        "external_subject_hash": (
            override_subject_hash if override_subject_hash is not None else _hash_text(user.external_subject)
        ),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _get_user_by_id(session: Session, user_id: UUID) -> User | None:
    result = session.execute(select(User).where(user_id == USER_ID_COLUMN).limit(1))
    return result.scalar_one_or_none()


def _get_user_by_username(session: Session, username: str) -> User | None:
    result = session.execute(select(User).where(username == USER_USERNAME_COLUMN).limit(1))
    return result.scalar_one_or_none()


def _get_user_by_external_principal_id(session: Session, principal_id: str) -> User | None:
    result = session.execute(select(User).where(principal_id == USER_EXTERNAL_PRINCIPAL_ID_COLUMN).limit(1))
    return result.scalar_one_or_none()


def _get_user_by_external_identity(session: Session, issuer: str, subject: str) -> User | None:
    result = session.execute(
        select(User)
        .where(
            and_(
                issuer == USER_EXTERNAL_ISSUER_COLUMN,
                subject == USER_EXTERNAL_SUBJECT_COLUMN,
            )
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind existing local registry users to explicit OIDC principals before cutover.",
    )
    parser.add_argument(
        "--mapping-file",
        type=Path,
        required=True,
        help="JSON array of explicit user-to-principal mappings.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the linkage. Omit to run in dry-run mode.",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Optional path to write the JSON report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        mappings = load_link_mappings(args.mapping_file)
    except ValueError as exc:
        _write_line(sys.stderr, f"[ERROR] {exc}")
        return 2

    try:
        with get_sync_session() as session:
            report = migrate_local_users_to_oidc(
                session,
                mappings,
                dry_run=not args.apply,
            )
    except Exception as exc:  # pragma: no cover - exercised via real command usage
        _write_line(sys.stderr, f"[ERROR] {exc}")
        return 1
    finally:
        close_sync_engine()

    payload = json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2)
    _write_line(sys.stdout, payload)
    if args.report_file is not None:
        args.report_file.write_text(payload + "\n", encoding="utf-8")

    if args.apply and report.blocking_count > 0:
        _write_line(sys.stderr, "[ERROR] apply requested but the report still contains blocking items")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
