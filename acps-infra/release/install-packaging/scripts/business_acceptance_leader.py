"""业务验收步骤 B/C 共享 Leader Web API 客户端。

改编自 demo-leader / acps-infra smoke-test-business happy path。
与遗留 smoke 的差异：
  - LLM 错误不得跳过成功（带分类错误失败）。
  - 可选 Bearer token（local auth = 无 token；Keycloak OIDC = token 文件）。
  - 仅对瞬时 LLM / 网络故障有限重试。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LLM_ERROR_CODES = frozenset({"LLM_CALL_ERROR", "LLM_SERVICE_UNAVAILABLE", "LLM_PARSE_ERROR"})

GREETING_QUERY = "你好。"
# Turn2 故意省略 hotel/transport 必填槽（人数、入住晚数、出发城、明确城际日期），
# 以提高 china_hotel / china_transport 进入 AwaitingInput 的命中率。
TRAVEL_PLAN_QUERY = "请帮我规划北京游，需要景点、美食、酒店和城际交通建议。"
# Turn3 精确补齐 skills 缺口 + 少量偏好（供 TASK_INPUT 合法收敛）。
TRAVEL_SUPPLEMENT_QUERY = (
    "两人出行；酒店 2026-05-01 入住、住两晚、希望朝阳区；"
    "城际交通上海往返北京，5月1日去、5月3日返程；"
    "晚餐偏北京菜，预算可到 6000。"
)
WARNING_SKIP_SUPPLEMENT_PREFIX = (
    "WARNING: skip supplement — no AwaitingInput partners"
)


class LeaderClientError(RuntimeError):
    """Base failure for Leader API acceptance (network / HTTP / assertion)."""

    category: str = "assertion"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category:
            self.category = category


class LlmError(LeaderClientError):
    category = "llm"


class NetworkError(LeaderClientError):
    category = "network"


def _log(prefix: str, message: str) -> None:
    print(f"[{prefix}] {message}", flush=True)


def load_access_token(token_file: str | Path | None) -> str | None:
    if not token_file:
        return None
    path = Path(token_file)
    if not path.is_file():
        raise LeaderClientError(f"token file missing: {path}", category="auth")
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("access_token")
    if not token:
        raise LeaderClientError(f"missing access_token in {path}", category="auth")
    return str(token)


def member_field(member: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in member:
            return member[name]
    return None


def invitation_route_of(member: dict[str, Any]) -> str | None:
    value = member_field(member, "invitationRoute", "invitation_route")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def partner_aic_of(member: dict[str, Any]) -> str | None:
    value = member_field(member, "partnerAic", "partner_aic")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_llm_code(parsed: dict[str, Any] | None) -> str | None:
    if not isinstance(parsed, dict):
        return None
    detail = parsed.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        if code in LLM_ERROR_CODES:
            return str(code)
    error = parsed.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code in LLM_ERROR_CODES:
            return str(code)
    return None


class LeaderApiClient:
    """Minimal Leader `/api/v1` client used by Steps B and C."""

    def __init__(
        self,
        *,
        api_base_url: str,
        token: str | None = None,
        token_file: str | Path | None = None,
        http_timeout: float = 180.0,
        llm_retries: int = 2,
        llm_retry_delay: float = 3.0,
        log_prefix: str = "biz-accept",
    ) -> None:
        base = api_base_url.rstrip("/")
        if urlsplit(base).scheme.lower() not in {"http", "https"}:
            raise LeaderClientError(f"unsupported API base URL: {base}", category="network")
        self.api_base_url = base
        self.token_file = Path(token_file) if token_file else None
        self.token = token or (load_access_token(self.token_file) if self.token_file else None)
        self.http_timeout = http_timeout
        self.llm_retries = max(0, int(llm_retries))
        self.llm_retry_delay = max(0.0, float(llm_retry_delay))
        self.log_prefix = log_prefix

    def log(self, message: str) -> None:
        _log(self.log_prefix, message)

    def _refresh_token_from_file(self) -> None:
        if self.token_file is None:
            return
        self.token = load_access_token(self.token_file)

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any] | None, str]:
        # 每次调用从磁盘重载 OIDC token，使长轮询熬过 Keycloak TTL
        # 当 ansible 在重试间刷新 token 文件时。
        self._refresh_token_from_file()
        url = path if path.startswith("http") else f"{self.api_base_url}/{path.lstrip('/')}"
        body: bytes | None = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with urlopen(request, timeout=timeout or self.http_timeout) as response:  # noqa: S310
                status = int(response.status)
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            status = int(exc.code)
            raw_body = exc.read().decode("utf-8", errors="replace")
        except TimeoutError as exc:
            raise NetworkError(f"HTTP timeout: {method} {url}") from exc
        except URLError as exc:
            raise NetworkError(f"HTTP failed: {method} {url}: {exc.reason}") from exc

        parsed: dict[str, Any] | None
        try:
            loaded = json.loads(raw_body) if raw_body else None
            parsed = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            parsed = None
        return status, parsed, raw_body

    def request_json_with_llm_retry(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        context: str,
    ) -> tuple[int, dict[str, Any] | None, str]:
        attempts = self.llm_retries + 1
        last_status = 0
        last_parsed: dict[str, Any] | None = None
        last_raw = ""
        for attempt in range(1, attempts + 1):
            try:
                status, parsed, raw_body = self.request_json(method, path, payload)
            except NetworkError as exc:
                if attempt >= attempts:
                    raise
                self.log(f"{context}: network retry {attempt}/{attempts}: {exc}")
                time.sleep(self.llm_retry_delay * attempt)
                continue

            last_status, last_parsed, last_raw = status, parsed, raw_body
            llm_code = extract_llm_code(parsed) if status >= 500 else None
            if llm_code is None:
                return status, parsed, raw_body
            if attempt >= attempts:
                raise LlmError(f"{context}: {llm_code} (status={status}): {raw_body[:800]}")
            self.log(f"{context}: LLM retry {attempt}/{attempts}: {llm_code}")
            time.sleep(self.llm_retry_delay * attempt)
        raise LlmError(f"{context}: exhausted retries status={last_status} body={last_raw[:800]}")

    @staticmethod
    def ensure(condition: bool, message: str, *, category: str = "assertion") -> None:
        if not condition:
            raise LeaderClientError(message, category=category)

    @staticmethod
    def new_request_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def submit(
        self,
        query: str,
        mode: str,
        session_id: str | None = None,
        active_task_id: str | None = None,
    ) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "clientRequestId": self.new_request_id(mode),
        }
        if session_id:
            payload["sessionId"] = session_id
        if active_task_id:
            payload["activeTaskId"] = active_task_id

        status, parsed, raw_body = self.request_json_with_llm_retry(
            "POST",
            "submit",
            payload,
            context=f"submit[{mode}]",
        )
        self.ensure(status == 200, f"submit[{mode}] returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"submit[{mode}] non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"submit[{mode}] missing result: {parsed}")
        assert isinstance(result, dict)
        self.ensure(result.get("mode") == mode, f"submit[{mode}] mode mismatch: {result.get('mode')}")
        for field in ("sessionId", "activeTaskId", "acceptedAt", "externalStatus"):
            self.ensure(field in result and result[field], f"submit[{mode}] missing {field}: {result}")
        return str(result["sessionId"]), str(result["activeTaskId"])

    @staticmethod
    def extract_group_id(result: dict[str, Any], active_task: dict[str, Any]) -> str | None:
        for candidate in (
            result.get("groupId"),
            result.get("group_id"),
            active_task.get("groupId"),
            active_task.get("group_id"),
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    @staticmethod
    def partner_task_states(partner_tasks: dict[str, Any]) -> dict[str, str]:
        """Normalize partnerTasks[*].state for acceptance gates."""
        states: dict[str, str] = {}
        for aic, task in partner_tasks.items():
            if not isinstance(task, dict):
                continue
            raw = task.get("state")
            if raw is None and isinstance(task.get("status"), dict):
                raw = task["status"].get("state")
            if raw is None:
                continue
            states[str(aic)] = str(raw)
        return states

    @staticmethod
    def is_awaiting_input_state(state: str | None) -> bool:
        if not state:
            return False
        normalized = state.strip().lower().replace("_", "-")
        return normalized in {"awaiting-input", "awaitinginput"}

    @classmethod
    def needs_task_input_supplement(cls, snapshot: dict[str, Any]) -> bool:
        """True only when TASK_INPUT/supplement is still valid.

        Leader rejects supplement with「没有找到等待输入的 Partner」when no partner
        remains in AwaitingInput (e.g. turn already reached AwaitingCompletion/Completed).
        """
        if int(snapshot.get("awaiting_input_count") or 0) > 0:
            return True
        # Clarification without explicit states still implies AwaitingInput path.
        if snapshot.get("result_type") == "clarification":
            states = snapshot.get("partner_states") or {}
            if not states:
                return True
            # If states are present but none awaiting-input, clarification is stale.
            return any(cls.is_awaiting_input_state(s) for s in states.values())
        return False

    def poll_result(
        self,
        session_id: str,
        expected_mode: str,
        timeout_seconds: float,
        interval_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_snapshot: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status, parsed, raw_body = self.request_json_with_llm_retry(
                "GET",
                f"result/{session_id}",
                context=f"result[{expected_mode}]",
            )
            self.ensure(status == 200, f"result[{expected_mode}] returned {status}: {raw_body}")
            self.ensure(isinstance(parsed, dict), f"result[{expected_mode}] non-JSON: {raw_body}")
            assert parsed is not None
            result = parsed.get("result")
            self.ensure(isinstance(result, dict), f"result[{expected_mode}] missing result: {parsed}")
            assert isinstance(result, dict)
            self.ensure(
                result.get("sessionId") == session_id,
                f"result[{expected_mode}] sessionId mismatch",
            )
            self.ensure(
                result.get("mode") == expected_mode,
                f"result[{expected_mode}] mode mismatch: {result.get('mode')}",
            )

            user_result = result.get("userResult") or {}
            active_task = result.get("activeTask") or {}
            partner_tasks = active_task.get("partnerTasks") or {}
            dialog_context = result.get("dialogContext") or {}
            recent_turns = dialog_context.get("recentTurns") or []
            group_id = self.extract_group_id(result, active_task if isinstance(active_task, dict) else {})
            partner_states = self.partner_task_states(
                partner_tasks if isinstance(partner_tasks, dict) else {}
            )

            last_snapshot = {
                "result_type": user_result.get("type") if isinstance(user_result, dict) else None,
                "session_id": result.get("sessionId"),
                "active_task_id": active_task.get("activeTaskId") if isinstance(active_task, dict) else None,
                "active_task_status": (
                    active_task.get("externalStatus") if isinstance(active_task, dict) else None
                ),
                "partner_task_count": len(partner_tasks) if isinstance(partner_tasks, dict) else 0,
                "partner_states": partner_states,
                "awaiting_input_count": sum(
                    1 for state in partner_states.values() if self.is_awaiting_input_state(state)
                ),
                "dialog_turns": len(recent_turns) if isinstance(recent_turns, list) else 0,
                "group_id": group_id,
                "closed": bool(result.get("closed")),
            }

            if last_snapshot["result_type"] in {"final", "clarification", "error"}:
                if last_snapshot["result_type"] == "error":
                    raise LlmError(
                        f"result[{expected_mode}] converged to error: {last_snapshot}"
                    )
                return last_snapshot
            time.sleep(interval_seconds)

        raise LeaderClientError(
            f"poll timeout: mode={expected_mode} session={session_id} last={last_snapshot}"
        )

    def cancel_session(self, session_id: str) -> None:
        status, parsed, raw_body = self.request_json("POST", f"cancel/{session_id}")
        self.ensure(status == 200, f"cancel returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"cancel non-JSON: {raw_body}")

        status, parsed, raw_body = self.request_json("GET", f"result/{session_id}")
        self.ensure(status == 200, f"post-cancel result returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"post-cancel result non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"post-cancel result empty: {parsed}")
        assert isinstance(result, dict)
        self.ensure(bool(result.get("closed")), f"post-cancel closed not true: {result}")

    def cancel_and_delete_session(self, session_id: str) -> None:
        status, parsed, raw_body = self.request_json(
            "POST",
            f"cancel/{session_id}",
            {"deleteSession": True},
        )
        self.ensure(status == 200, f"cancel(delete) returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"cancel(delete) non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"cancel(delete) missing result: {parsed}")
        assert isinstance(result, dict)
        self.ensure(result.get("sessionDeleted") is True, f"session not deleted: {result}")

        status, _, raw_body = self.request_json("GET", f"result/{session_id}")
        self.ensure(status == 404, f"deleted session still queryable: {status} {raw_body}")

        status, _, raw_body = self.request_json("GET", f"group/{session_id}")
        self.ensure(status == 404, f"deleted group runtime still queryable: {status} {raw_body}")

    def get_group_runtime(self, session_id: str) -> dict[str, Any]:
        status, parsed, raw_body = self.request_json("GET", f"group/{session_id}")
        self.ensure(status == 200, f"group runtime returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"group runtime non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"group runtime missing result: {parsed}")
        assert isinstance(result, dict)
        return result

    @staticmethod
    def summarize_group_runtime(runtime: dict[str, Any]) -> str:
        members = runtime.get("members") or []
        member_parts = []
        for member in members:
            if not isinstance(member, dict):
                continue
            member_parts.append(
                f"{partner_aic_of(member)}:route={invitation_route_of(member)},"
                f"connected={member.get('connected')},"
                f"queue={member_field(member, 'queueName', 'queue_name')}"
            )
        return (
            f"groupId={runtime.get('groupId') or runtime.get('group_id')} "
            f"state={runtime.get('state')} "
            f"connected={runtime.get('connectedMembers') or runtime.get('connected_members')}/"
            f"{runtime.get('totalMembers') or runtime.get('total_members')} "
            f"pending={runtime.get('pendingInvitations') or runtime.get('pending_invitations')} "
            f"members=[{'; '.join(member_parts)}]"
        )

    @staticmethod
    def find_group_member(runtime: dict[str, Any], partner_aic: str) -> dict[str, Any] | None:
        for member in runtime.get("members") or []:
            if isinstance(member, dict) and partner_aic_of(member) == partner_aic:
                return member
        return None

    def assert_inbox_invitation_routes(self, runtime: dict[str, Any], *, context: str) -> None:
        """Hard gate: every member must use inbox; any rpc route fails."""
        members = runtime.get("members") or []
        if not members:
            raise LeaderClientError(f"{context}: group runtime has no members: {runtime}")
        bad: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                bad.append(f"<non-dict>:{member!r}")
                continue
            route = invitation_route_of(member)
            aic = partner_aic_of(member) or "<unknown>"
            if route != "inbox":
                bad.append(f"{aic}:route={route!r}")
        if bad:
            raise LeaderClientError(
                f"{context}: invitationRoute must be 'inbox' for all members "
                f"(RPC invite is a hard failure). bad=[{', '.join(bad)}] "
                f"runtime={self.summarize_group_runtime(runtime)}"
            )

    def wait_for_group_runtime(
        self,
        session_id: str,
        description: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float,
        interval_seconds: float,
        require_inbox: bool = True,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_runtime: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            runtime = self.get_group_runtime(session_id)
            last_runtime = runtime
            # 任一成员已选择 RPC 邀请时立即失败。
            if require_inbox:
                members = runtime.get("members") or []
                rpc_members = [
                    partner_aic_of(m) or "?"
                    for m in members
                    if isinstance(m, dict) and invitation_route_of(m) == "rpc"
                ]
                if rpc_members:
                    raise LeaderClientError(
                        f"{description}: RPC invitation observed for "
                        f"{', '.join(rpc_members)}; inbox required "
                        f"(). {self.summarize_group_runtime(runtime)}"
                    )
            if predicate(runtime):
                if require_inbox:
                    self.assert_inbox_invitation_routes(runtime, context=description)
                self.log(f"{description}: {self.summarize_group_runtime(runtime)}")
                return runtime
            time.sleep(interval_seconds)

        raise LeaderClientError(
            f"{description} timeout: session={session_id} "
            f"last={self.summarize_group_runtime(last_runtime or {})}"
        )

    def request_group_member_leave(self, session_id: str, partner_aic: str) -> None:
        status, parsed, raw_body = self.request_json(
            "POST",
            f"group/{session_id}/members/{partner_aic}/leave",
        )
        self.ensure(status == 200, f"request leave returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"request leave non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"request leave missing result: {parsed}")
        assert isinstance(result, dict)
        self.ensure(result.get("action") == "request-leave", f"request leave action unexpected: {result}")

    def force_remove_group_member(self, session_id: str, partner_aic: str) -> None:
        status, parsed, raw_body = self.request_json(
            "DELETE",
            f"group/{session_id}/members/{partner_aic}",
        )
        self.ensure(status == 200, f"force remove returned {status}: {raw_body}")
        self.ensure(isinstance(parsed, dict), f"force remove non-JSON: {raw_body}")
        assert parsed is not None
        result = parsed.get("result")
        self.ensure(isinstance(result, dict), f"force remove missing result: {parsed}")
        assert isinstance(result, dict)
        self.ensure(result.get("action") == "force-remove", f"force remove action unexpected: {result}")


def add_common_leader_args(parser: Any) -> None:
    parser.add_argument("--api-base-url", required=True, help="Leader Web API base, e.g. http://host:9030/api/v1")
    parser.add_argument(
        "--token-file",
        default="",
        help="OIDC access_token JSON (required when Keycloak/OIDC on; omit for local auth)",
    )
    parser.add_argument("--http-timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--llm-retries",
        type=int,
        default=2,
        help="Extra attempts on LLM_CALL_ERROR / LLM_SERVICE_UNAVAILABLE / LLM_PARSE_ERROR (never skip-success)",
    )
    parser.add_argument("--llm-retry-delay-seconds", type=float, default=3.0)


def build_client_from_args(args: Any, *, log_prefix: str) -> LeaderApiClient:
    token_file = getattr(args, "token_file", "") or None
    return LeaderApiClient(
        api_base_url=args.api_base_url,
        token_file=token_file,
        http_timeout=args.http_timeout_seconds,
        llm_retries=args.llm_retries,
        llm_retry_delay=args.llm_retry_delay_seconds,
        log_prefix=log_prefix,
    )
