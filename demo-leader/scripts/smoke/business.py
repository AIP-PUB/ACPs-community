"""业务 happy path 冒烟测试：direct_rpc / group 两种模式的完整 API 验证。

通过环境变量配置：
    API_BASE_URL          Web API 基础路径（必填）
    RPC_POLL_INTERVAL     direct_rpc 轮询间隔（秒，默认 1）
    RPC_POLL_TIMEOUT      direct_rpc 轮询超时（秒，默认 120）
    TASK_POLL_TIMEOUT     任务轮询超时（秒，默认 180）
    GROUP_POLL_INTERVAL   group 轮询间隔（秒，默认 2）
    GROUP_POLL_TIMEOUT    group 轮询超时（秒，默认 180）
    HTTP_REQUEST_TIMEOUT  单次 HTTP 请求超时（秒，默认 180）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")
RPC_POLL_INTERVAL = float(os.environ.get("RPC_POLL_INTERVAL", "1"))
RPC_POLL_TIMEOUT = int(os.environ.get("RPC_POLL_TIMEOUT", "120"))
TASK_POLL_TIMEOUT = int(os.environ.get("TASK_POLL_TIMEOUT", "180"))
GROUP_POLL_INTERVAL = float(os.environ.get("GROUP_POLL_INTERVAL", "2"))
GROUP_POLL_TIMEOUT = int(os.environ.get("GROUP_POLL_TIMEOUT", "300"))
HTTP_REQUEST_TIMEOUT = int(os.environ.get("HTTP_REQUEST_TIMEOUT", "180"))
GROUP_MIN_MEMBERS = int(os.environ.get("GROUP_MIN_MEMBERS", "2"))
LLM_ERROR_CODES = {"LLM_CALL_ERROR", "LLM_SERVICE_UNAVAILABLE", "LLM_PARSE_ERROR"}
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
WARNING_SKIP_SUPPLEMENT_PREFIX = "WARNING: skip supplement — no AwaitingInput partners"


class SmokeError(Exception):
    pass


class SkipBusinessError(Exception):
    pass


def log(message: str) -> None:
    print(f"[smoke-test-business] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[smoke-test-business] WARN: {message}", file=sys.stderr, flush=True)


def request_json(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: int = HTTP_REQUEST_TIMEOUT,
) -> tuple[int, dict | None, str]:
    url = path if path.startswith("http") else f"{API_BASE_URL}/{path.lstrip('/')}"
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise SmokeError(f"不支持的 URL scheme: {url}")

    body: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310  # nosec B310 - URL scheme is validated above
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - URL scheme is validated above
            status = response.status
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw_body = exc.read().decode("utf-8")
    except TimeoutError as exc:
        raise SmokeError(f"HTTP 请求超时: {method} {url}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"HTTP 请求失败: {method} {url}: {exc.reason}") from exc

    try:
        parsed = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed, raw_body


def maybe_skip_for_llm(status: int, parsed: dict | None, context: str) -> None:
    if status != 500 or not isinstance(parsed, dict):
        return
    detail = parsed.get("detail")
    if isinstance(detail, dict) and detail.get("code") in LLM_ERROR_CODES:
        raise SkipBusinessError(f"{context}: {detail['code']}")
    error = parsed.get("error")
    if isinstance(error, dict) and error.get("code") in LLM_ERROR_CODES:
        raise SkipBusinessError(f"{context}: {error['code']}")


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def new_request_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def summarize_group_runtime(runtime: dict) -> str:
    members = runtime.get("members") or []
    member_parts = []
    for member in members:
        member_parts.append(
            f"{member.get('partnerAic')}:route={member.get('invitationRoute')},"
            f"connected={member.get('connected')},queue={member.get('queueName')}"
        )
    return (
        f"groupId={runtime.get('groupId')} state={runtime.get('state')} "
        f"connected={runtime.get('connectedMembers')}/{runtime.get('totalMembers')} "
        f"pending={runtime.get('pendingInvitations')} members=[{'; '.join(member_parts)}]"
    )


def find_group_member(runtime: dict, partner_aic: str) -> dict | None:
    for member in runtime.get("members") or []:
        if member.get("partnerAic") == partner_aic:
            return member
    return None


def extract_group_id(result: dict, active_task: dict) -> str | None:
    for candidate in (
        result.get("groupId"),
        result.get("group_id"),
        active_task.get("groupId"),
        active_task.get("group_id"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def partner_task_states(partner_tasks: dict) -> dict[str, str]:
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


def is_awaiting_input_state(state: str | None) -> bool:
    if not state:
        return False
    normalized = state.strip().lower().replace("_", "-")
    return normalized in {"awaiting-input", "awaitinginput"}


def needs_task_input_supplement(snapshot: dict) -> bool:
    """True only when TASK_INPUT/supplement is still valid."""
    if int(snapshot.get("awaiting_input_count") or 0) > 0:
        return True
    if snapshot.get("result_type") == "clarification":
        states = snapshot.get("partner_states") or {}
        if not states:
            return True
        return any(is_awaiting_input_state(s) for s in states.values())
    return False


def submit(
    query: str,
    mode: str,
    session_id: str | None = None,
    active_task_id: str | None = None,
) -> tuple[str, str]:
    payload: dict = {
        "query": query,
        "mode": mode,
        "clientRequestId": new_request_id(mode),
    }
    if session_id:
        payload["sessionId"] = session_id
    if active_task_id:
        payload["activeTaskId"] = active_task_id

    status, parsed, raw_body = request_json("POST", "submit", payload)
    maybe_skip_for_llm(status, parsed, f"submit[{mode}]")
    ensure(status == 200, f"submit[{mode}] 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"submit[{mode}] 返回非 JSON: {raw_body}")

    result = parsed.get("result")
    ensure(isinstance(result, dict), f"submit[{mode}] 缺少 result: {parsed}")
    ensure(result.get("mode") == mode, f"submit[{mode}] mode 不匹配: {result.get('mode')}")
    for field in ("sessionId", "activeTaskId", "acceptedAt", "externalStatus"):
        ensure(
            field in result and result[field],
            f"submit[{mode}] 缺少字段 {field}: {result}",
        )
    return result["sessionId"], result["activeTaskId"]


def poll_result(
    session_id: str,
    expected_mode: str,
    timeout_seconds: int,
    interval_seconds: float,
) -> dict:
    deadline = time.time() + timeout_seconds
    last_snapshot: dict | None = None
    while time.time() < deadline:
        status, parsed, raw_body = request_json("GET", f"result/{session_id}")
        maybe_skip_for_llm(status, parsed, f"result[{expected_mode}]")
        ensure(status == 200, f"result[{expected_mode}] 返回 {status}: {raw_body}")
        ensure(isinstance(parsed, dict), f"result[{expected_mode}] 返回非 JSON: {raw_body}")

        result = parsed.get("result")
        ensure(isinstance(result, dict), f"result[{expected_mode}] 缺少 result: {parsed}")
        ensure(
            result.get("sessionId") == session_id,
            f"result[{expected_mode}] sessionId 不匹配",
        )
        ensure(
            result.get("mode") == expected_mode,
            f"result[{expected_mode}] mode 不匹配: {result.get('mode')}",
        )

        user_result = result.get("userResult") or {}
        active_task = result.get("activeTask") or {}
        partner_tasks = active_task.get("partnerTasks") or {}
        dialog_context = result.get("dialogContext") or {}
        recent_turns = dialog_context.get("recentTurns") or []
        group_id = extract_group_id(result, active_task)
        partner_states = partner_task_states(partner_tasks)
        awaiting_input_count = sum(1 for state in partner_states.values() if is_awaiting_input_state(state))

        last_snapshot = {
            "result_type": user_result.get("type"),
            "session_id": result.get("sessionId"),
            "active_task_id": active_task.get("activeTaskId"),
            "active_task_status": active_task.get("externalStatus"),
            "partner_task_count": len(partner_tasks),
            "dialog_turns": len(recent_turns),
            "group_id": group_id,
            "closed": bool(result.get("closed")),
            "partner_states": partner_states,
            "awaiting_input_count": awaiting_input_count,
        }

        if last_snapshot["result_type"] in {"final", "clarification", "error"}:
            return last_snapshot

        time.sleep(interval_seconds)

    raise SmokeError(f"轮询超时: mode={expected_mode}, session={session_id}, last={last_snapshot}")


def cancel_session(session_id: str) -> None:
    status, parsed, raw_body = request_json("POST", f"cancel/{session_id}")
    ensure(status == 200, f"cancel 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"cancel 返回非 JSON: {raw_body}")

    status, parsed, raw_body = request_json("GET", f"result/{session_id}")
    ensure(status == 200, f"cancel 后查询 result 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"cancel 后 result 返回非 JSON: {raw_body}")
    result = parsed.get("result")
    ensure(isinstance(result, dict), f"cancel 后 result 为空: {parsed}")
    ensure(bool(result.get("closed")), f"cancel 后 closed 未置为 true: {result}")


def cancel_and_delete_session(session_id: str) -> None:
    status, parsed, raw_body = request_json(
        "POST",
        f"cancel/{session_id}",
        {"deleteSession": True},
    )
    ensure(status == 200, f"cancel(delete) 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"cancel(delete) 返回非 JSON: {raw_body}")
    result = parsed.get("result")
    ensure(isinstance(result, dict), f"cancel(delete) 缺少 result: {parsed}")
    ensure(result.get("sessionDeleted") is True, f"session 未删除: {result}")

    status, _, raw_body = request_json("GET", f"result/{session_id}")
    ensure(status == 404, f"删除后 result 仍可查询: {status} {raw_body}")

    status, _, raw_body = request_json("GET", f"group/{session_id}")
    ensure(status == 404, f"删除后 group runtime 仍可查询: {status} {raw_body}")


def get_group_runtime(session_id: str) -> dict:
    status, parsed, raw_body = request_json("GET", f"group/{session_id}")
    ensure(status == 200, f"group runtime 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"group runtime 返回非 JSON: {raw_body}")
    result = parsed.get("result")
    ensure(isinstance(result, dict), f"group runtime 缺少 result: {parsed}")
    return result


def wait_for_group_runtime(
    session_id: str,
    description: str,
    predicate,
    timeout_seconds: int = GROUP_POLL_TIMEOUT,
    interval_seconds: float = GROUP_POLL_INTERVAL,
) -> dict:
    deadline = time.time() + timeout_seconds
    last_runtime: dict | None = None
    while time.time() < deadline:
        runtime = get_group_runtime(session_id)
        last_runtime = runtime
        if predicate(runtime):
            log(f"{description}: {summarize_group_runtime(runtime)}")
            return runtime
        time.sleep(interval_seconds)

    raise SmokeError(f"{description} 超时: session={session_id}, last={summarize_group_runtime(last_runtime or {})}")


def request_group_member_leave(session_id: str, partner_aic: str) -> None:
    status, parsed, raw_body = request_json(
        "POST",
        f"group/{session_id}/members/{partner_aic}/leave",
    )
    ensure(status == 200, f"request leave 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"request leave 返回非 JSON: {raw_body}")
    result = parsed.get("result")
    ensure(isinstance(result, dict), f"request leave 缺少 result: {parsed}")
    ensure(result.get("action") == "request-leave", f"request leave 动作异常: {result}")


def force_remove_group_member(session_id: str, partner_aic: str) -> None:
    status, parsed, raw_body = request_json(
        "DELETE",
        f"group/{session_id}/members/{partner_aic}",
    )
    ensure(status == 200, f"force remove 返回 {status}: {raw_body}")
    ensure(isinstance(parsed, dict), f"force remove 返回非 JSON: {raw_body}")
    result = parsed.get("result")
    ensure(isinstance(result, dict), f"force remove 缺少 result: {parsed}")
    ensure(result.get("action") == "force-remove", f"force remove 动作异常: {result}")


def is_group_runtime_ready(current: dict, expected_group_id: str | None) -> bool:
    if expected_group_id:
        if current.get("groupId") != expected_group_id:
            return False
    elif not current.get("groupId"):
        return False

    members = current.get("members") or []
    if len(members) < GROUP_MIN_MEMBERS:
        return False
    if current.get("connectedMembers") != len(members):
        return False
    if current.get("pendingInvitations"):
        return False
    return all(member.get("invitationRoute") == "inbox" for member in members)


def is_group_runtime_stable(current: dict, member_aics: list[str]) -> bool:
    return all(find_group_member(current, partner_aic) is not None for partner_aic in member_aics) and all(
        (find_group_member(current, partner_aic) or {}).get("invitationRoute") == "inbox" for partner_aic in member_aics
    )


def has_graceful_leaves(
    current: dict,
    graceful_members: list[str],
    force_member: str,
) -> bool:
    graceful_left = all(
        ((member := find_group_member(current, partner_aic)) is not None and member.get("connected") is False)
        for partner_aic in graceful_members
    )
    force_member_connected = (find_group_member(current, force_member) or {}).get("connected") is True
    return graceful_left and force_member_connected


def has_force_remove(
    current: dict,
    graceful_members: list[str],
    force_member: str,
) -> bool:
    graceful_left = all(
        ((member := find_group_member(current, partner_aic)) is not None and member.get("connected") is False)
        for partner_aic in graceful_members
    )
    return find_group_member(current, force_member) is None and graceful_left


def run_direct_rpc() -> None:
    log("direct_rpc turn 1: greeting")
    session_id, _ = submit(
        query=GREETING_QUERY,
        mode="direct_rpc",
    )
    snapshot = poll_result(session_id, "direct_rpc", RPC_POLL_TIMEOUT, RPC_POLL_INTERVAL)
    ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"direct_rpc turn 1 未收敛: {snapshot}",
    )
    ensure(snapshot["dialog_turns"] >= 1, f"direct_rpc turn 1 对话轮次异常: {snapshot}")
    if snapshot["partner_task_count"] >= 1:
        warn("direct_rpc turn 1 已进入任务澄清流程，按实际运行结果继续后续多轮 happy path")

    log("direct_rpc turn 2: create task")
    session_id_again, _ = submit(
        query=TRAVEL_PLAN_QUERY,
        mode="direct_rpc",
        session_id=session_id,
        active_task_id=snapshot["active_task_id"],
    )
    ensure(session_id_again == session_id, "direct_rpc turn 2 sessionId 发生变化")
    snapshot = poll_result(session_id, "direct_rpc", TASK_POLL_TIMEOUT, RPC_POLL_INTERVAL)
    ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"direct_rpc turn 2 未收敛: {snapshot}",
    )
    ensure(snapshot["dialog_turns"] >= 2, f"direct_rpc turn 2 对话轮次异常: {snapshot}")
    ensure(
        snapshot["partner_task_count"] >= 1,
        f"direct_rpc turn 2 未观察到 partnerTasks: {snapshot}",
    )
    log(
        "direct_rpc turn 2 state: "
        f"result_type={snapshot.get('result_type')} "
        f"awaiting_input={snapshot.get('awaiting_input_count')} "
        f"partner_states={snapshot.get('partner_states')}"
    )

    if needs_task_input_supplement(snapshot):
        log("direct_rpc turn 3: supplement task")
        session_id_again, _ = submit(
            query=TRAVEL_SUPPLEMENT_QUERY,
            mode="direct_rpc",
            session_id=session_id,
            active_task_id=snapshot["active_task_id"],
        )
        ensure(session_id_again == session_id, "direct_rpc turn 3 sessionId 发生变化")
        snapshot = poll_result(session_id, "direct_rpc", TASK_POLL_TIMEOUT, RPC_POLL_INTERVAL)
        ensure(
            snapshot["result_type"] in {"final", "clarification"},
            f"direct_rpc turn 3 未收敛: {snapshot}",
        )
        ensure(snapshot["dialog_turns"] >= 3, f"direct_rpc turn 3 对话轮次异常: {snapshot}")
    else:
        warn(
            f"{WARNING_SKIP_SUPPLEMENT_PREFIX} "
            f"(result_type={snapshot.get('result_type')}, "
            f"states={snapshot.get('partner_states')}); continue lifecycle"
        )

    log("direct_rpc turn 4: cancel session")
    cancel_session(session_id)


def run_group() -> None:
    log("group turn 1: greeting")
    session_id, _ = submit(
        query=GREETING_QUERY,
        mode="group",
    )
    snapshot = poll_result(session_id, "group", GROUP_POLL_TIMEOUT, GROUP_POLL_INTERVAL)
    ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"group turn 1 未收敛: {snapshot}",
    )
    ensure(snapshot["dialog_turns"] >= 1, f"group turn 1 对话轮次异常: {snapshot}")
    if snapshot["partner_task_count"] >= 1:
        warn("group turn 1 已进入任务澄清流程，按实际运行结果继续后续多轮 happy path")

    log("group turn 2: create task")
    session_id_again, _ = submit(
        query=TRAVEL_PLAN_QUERY,
        mode="group",
        session_id=session_id,
        active_task_id=snapshot["active_task_id"],
    )
    ensure(session_id_again == session_id, "group turn 2 sessionId 发生变化")
    snapshot = poll_result(session_id, "group", GROUP_POLL_TIMEOUT, GROUP_POLL_INTERVAL)
    ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"group turn 2 未收敛: {snapshot}",
    )
    ensure(snapshot["dialog_turns"] >= 2, f"group turn 2 对话轮次异常: {snapshot}")
    ensure(
        snapshot["partner_task_count"] >= 2,
        f"group turn 2 未观察到多个 partnerTasks: {snapshot}",
    )
    log(
        "group turn 2 state: "
        f"result_type={snapshot.get('result_type')} "
        f"awaiting_input={snapshot.get('awaiting_input_count')} "
        f"partner_states={snapshot.get('partner_states')}"
    )
    if not snapshot["group_id"]:
        warn(f"group turn 2 未在 result 中暴露 groupId，改用 /group 运行态补齐: {snapshot}")

    # Decide / run supplement BEFORE the long inbox wait so partners are less
    # likely to leave AwaitingInput while we wait for invitation routes.
    if needs_task_input_supplement(snapshot):
        log("group turn 3: supplement task")
        session_id_again, _ = submit(
            query=TRAVEL_SUPPLEMENT_QUERY,
            mode="group",
            session_id=session_id,
            active_task_id=snapshot["active_task_id"],
        )
        ensure(session_id_again == session_id, "group turn 3 sessionId 发生变化")
        snapshot = poll_result(session_id, "group", GROUP_POLL_TIMEOUT, GROUP_POLL_INTERVAL)
        ensure(
            snapshot["result_type"] in {"final", "clarification"},
            f"group turn 3 未收敛: {snapshot}",
        )
        ensure(snapshot["dialog_turns"] >= 3, f"group turn 3 对话轮次异常: {snapshot}")
        ensure(
            snapshot["partner_task_count"] >= 2,
            f"group turn 3 未维持多个 partnerTasks: {snapshot}",
        )
    else:
        warn(
            f"{WARNING_SKIP_SUPPLEMENT_PREFIX} "
            f"(result_type={snapshot.get('result_type')}, "
            f"states={snapshot.get('partner_states')}); continue lifecycle"
        )

    expected_group_id = snapshot["group_id"]
    runtime = wait_for_group_runtime(
        session_id,
        "group turn 2 runtime ready",
        lambda current: is_group_runtime_ready(current, expected_group_id),
    )
    snapshot["group_id"] = snapshot["group_id"] or runtime.get("groupId")
    member_aics = [member["partnerAic"] for member in runtime["members"]]
    ensure(
        len(member_aics) >= GROUP_MIN_MEMBERS,
        f"group runtime 成员数不足，无法覆盖优雅退出/强制移除场景: {runtime}",
    )
    wait_for_group_runtime(
        session_id,
        "group turn 3 runtime stable",
        lambda current: is_group_runtime_stable(current, member_aics),
    )

    graceful_members = member_aics[:-1]
    force_member = member_aics[-1]
    log("group lifecycle: graceful leave for " + ", ".join(graceful_members) + f"; force remove {force_member}")

    for partner_aic in graceful_members:
        request_group_member_leave(session_id, partner_aic)

    wait_for_group_runtime(
        session_id,
        "group graceful leaves observed",
        lambda current: has_graceful_leaves(current, graceful_members, force_member),
    )

    force_remove_group_member(session_id, force_member)
    wait_for_group_runtime(
        session_id,
        "group force remove observed",
        lambda current: has_force_remove(current, graceful_members, force_member),
    )

    log("group turn 4: cancel and delete session")
    cancel_and_delete_session(session_id)


def run_business_scenarios() -> None:
    scenarios = [
        ("direct_rpc", run_direct_rpc),
        ("group", run_group),
    ]
    failures: list[str] = []
    skip_exception: SkipBusinessError | None = None

    log("parallel scenarios: direct_rpc + group")
    with ThreadPoolExecutor(max_workers=len(scenarios)) as executor:
        future_to_name = {executor.submit(func): name for name, func in scenarios}
        for future in as_completed(future_to_name):
            scenario_name = future_to_name[future]
            try:
                future.result()
                log(f"{scenario_name} scenario completed")
            except SkipBusinessError as exc:
                if skip_exception is None:
                    skip_exception = exc
            except SmokeError as exc:
                failures.append(f"{scenario_name}: {exc}")
            except Exception as exc:
                failures.append(f"{scenario_name}: unexpected error: {exc}")

    if failures:
        raise SmokeError("; ".join(failures))
    if skip_exception is not None:
        raise skip_exception


def main() -> int:
    if not API_BASE_URL:
        warn("API_BASE_URL 未设置")
        return 1
    try:
        run_business_scenarios()
        log("OK")
        return 0
    except SkipBusinessError as exc:
        warn(f"跳过业务测试: {exc}")
        return 0
    except SmokeError as exc:
        warn(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
