#!/usr/bin/env python3
"""业务验收步骤 C — Leader↔Partner 群组与 inbox 邀请。

Happy path（与遗留 smoke/business.py group 对齐）：
  greeting → multi-partner travel task →（仅当仍有 AwaitingInput 时）supplement
  → inbox 就绪 → graceful leave / force remove → cancel+delete session。

硬性规则：
  - 断言每个成员 invitationRoute == "inbox"（RPC 邀请 → 失败）。
  - 无独立 mq-auth CLI 探测（inbox 路径已证明 MQ 面）。
  - 仅有限 LLM 重试；LLM 错误不得跳过成功。
  - turn 2 后尽快判断 supplement（先于长 inbox wait），无 AwaitingInput 时
    WARNING 跳过 supplement（不 Fail）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from business_acceptance_leader import (  # noqa: E402
    GREETING_QUERY,
    TRAVEL_PLAN_QUERY,
    TRAVEL_SUPPLEMENT_QUERY,
    WARNING_SKIP_SUPPLEMENT_PREFIX,
    LeaderApiClient,
    LeaderClientError,
    LlmError,
    NetworkError,
    add_common_leader_args,
    build_client_from_args,
    invitation_route_of,
    partner_aic_of,
)


def _group_id(runtime: dict[str, Any]) -> str | None:
    value = runtime.get("groupId") or runtime.get("group_id")
    return str(value) if value else None


def _connected_members(runtime: dict[str, Any]) -> int:
    value = runtime.get("connectedMembers")
    if value is None:
        value = runtime.get("connected_members")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pending_invitations(runtime: dict[str, Any]) -> list[Any]:
    value = runtime.get("pendingInvitations")
    if value is None:
        value = runtime.get("pending_invitations")
    return list(value or [])


def is_group_runtime_ready(current: dict[str, Any], expected_group_id: str | None, min_members: int) -> bool:
    gid = _group_id(current)
    if expected_group_id:
        if gid != expected_group_id:
            return False
    elif not gid:
        return False

    members = [m for m in (current.get("members") or []) if isinstance(m, dict)]
    if len(members) < min_members:
        return False
    if _connected_members(current) != len(members):
        return False
    if _pending_invitations(current):
        return False
    return all(invitation_route_of(member) == "inbox" for member in members)


def is_group_runtime_stable(current: dict[str, Any], member_aics: list[str]) -> bool:
    return all(LeaderApiClient.find_group_member(current, aic) is not None for aic in member_aics) and all(
        invitation_route_of(LeaderApiClient.find_group_member(current, aic) or {}) == "inbox"
        for aic in member_aics
    )


def has_graceful_leaves(current: dict[str, Any], graceful_members: list[str], force_member: str) -> bool:
    graceful_left = all(
        (
            (member := LeaderApiClient.find_group_member(current, aic)) is not None
            and member.get("connected") is False
        )
        for aic in graceful_members
    )
    force_connected = (LeaderApiClient.find_group_member(current, force_member) or {}).get("connected") is True
    return graceful_left and force_connected


def has_force_remove(current: dict[str, Any], graceful_members: list[str], force_member: str) -> bool:
    graceful_left = all(
        (
            (member := LeaderApiClient.find_group_member(current, aic)) is not None
            and member.get("connected") is False
        )
        for aic in graceful_members
    )
    return LeaderApiClient.find_group_member(current, force_member) is None and graceful_left


def run_group(
    *,
    api_base_url: str,
    token_file: str,
    group_poll_interval: float,
    group_poll_timeout: float,
    group_min_members: int,
    http_timeout: float,
    llm_retries: int,
    llm_retry_delay: float,
) -> None:
    class _Args:
        pass

    args = _Args()
    args.api_base_url = api_base_url
    args.token_file = token_file
    args.http_timeout_seconds = http_timeout
    args.llm_retries = llm_retries
    args.llm_retry_delay_seconds = llm_retry_delay
    client = build_client_from_args(args, log_prefix="biz-accept-C")

    client.log("group turn 1: greeting")
    session_id, _ = client.submit(query=GREETING_QUERY, mode="group")
    snapshot = client.poll_result(session_id, "group", group_poll_timeout, group_poll_interval)
    client.ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"group turn 1 did not converge: {snapshot}",
    )
    client.ensure(snapshot["dialog_turns"] >= 1, f"group turn 1 dialog turns: {snapshot}")
    if snapshot["partner_task_count"] >= 1:
        client.log("turn 1 already entered partner clarification; continuing happy path")

    client.log("group turn 2: multi-partner travel task")
    session_id_again, _ = client.submit(
        query=TRAVEL_PLAN_QUERY,
        mode="group",
        session_id=session_id,
        active_task_id=snapshot["active_task_id"],
    )
    client.ensure(session_id_again == session_id, "group turn 2 sessionId changed")
    snapshot = client.poll_result(session_id, "group", group_poll_timeout, group_poll_interval)
    client.ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"group turn 2 did not converge: {snapshot}",
    )
    client.ensure(snapshot["dialog_turns"] >= 2, f"group turn 2 dialog turns: {snapshot}")
    client.ensure(
        snapshot["partner_task_count"] >= group_min_members,
        f"group turn 2 need >= {group_min_members} partnerTasks: {snapshot}",
    )
    client.log(
        "group turn 2 state: "
        f"result_type={snapshot.get('result_type')} "
        f"awaiting_input={snapshot.get('awaiting_input_count')} "
        f"partner_states={snapshot.get('partner_states')}"
    )
    if not snapshot["group_id"]:
        client.log(f"groupId not in result; will use GET /group runtime: {snapshot}")

    # Decide / run supplement BEFORE the long inbox wait so partners are less
    # likely to leave AwaitingInput while we wait for invitation routes.
    if LeaderApiClient.needs_task_input_supplement(snapshot):
        client.log("group turn 3: supplement task")
        session_id_again, _ = client.submit(
            query=TRAVEL_SUPPLEMENT_QUERY,
            mode="group",
            session_id=session_id,
            active_task_id=snapshot["active_task_id"],
        )
        client.ensure(session_id_again == session_id, "group turn 3 sessionId changed")
        snapshot = client.poll_result(session_id, "group", group_poll_timeout, group_poll_interval)
        client.ensure(
            snapshot["result_type"] in {"final", "clarification"},
            f"group turn 3 did not converge: {snapshot}",
        )
        client.ensure(snapshot["dialog_turns"] >= 3, f"group turn 3 dialog turns: {snapshot}")
        client.ensure(
            snapshot["partner_task_count"] >= group_min_members,
            f"group turn 3 did not keep >= {group_min_members} partnerTasks: {snapshot}",
        )
    else:
        client.log(
            f"{WARNING_SKIP_SUPPLEMENT_PREFIX} "
            f"(result_type={snapshot.get('result_type')}, "
            f"states={snapshot.get('partner_states')}); continue lifecycle"
        )

    expected_group_id = snapshot["group_id"]
    runtime = client.wait_for_group_runtime(
        session_id,
        "group turn 2 runtime ready (inbox)",
        lambda current: is_group_runtime_ready(current, expected_group_id, group_min_members),
        timeout_seconds=group_poll_timeout,
        interval_seconds=group_poll_interval,
        require_inbox=True,
    )
    # Explicit hard assert — also enforced inside wait_for_group_runtime.
    client.assert_inbox_invitation_routes(runtime, context="group turn 2 inbox gate")
    client.log(
        "inbox invitation confirmed for members: "
        + ", ".join(
            f"{partner_aic_of(m)}={invitation_route_of(m)}"
            for m in (runtime.get("members") or [])
            if isinstance(m, dict)
        )
    )

    snapshot["group_id"] = snapshot["group_id"] or _group_id(runtime)
    member_aics = [
        aic
        for m in (runtime.get("members") or [])
        if isinstance(m, dict) and (aic := partner_aic_of(m))
    ]
    client.ensure(
        len(member_aics) >= group_min_members,
        f"group runtime members insufficient for leave/remove: {runtime}",
    )

    client.wait_for_group_runtime(
        session_id,
        "group runtime stable before lifecycle (inbox)",
        lambda current: is_group_runtime_stable(current, member_aics),
        timeout_seconds=group_poll_timeout,
        interval_seconds=group_poll_interval,
        require_inbox=True,
    )

    graceful_members = member_aics[:-1]
    force_member = member_aics[-1]
    client.log(
        "group lifecycle: graceful leave for "
        + ", ".join(graceful_members)
        + f"; force remove {force_member}"
    )

    for partner_aic in graceful_members:
        client.request_group_member_leave(session_id, partner_aic)

    client.wait_for_group_runtime(
        session_id,
        "group graceful leaves observed",
        lambda current: has_graceful_leaves(current, graceful_members, force_member),
        timeout_seconds=group_poll_timeout,
        interval_seconds=group_poll_interval,
        require_inbox=False,
    )

    client.force_remove_group_member(session_id, force_member)
    client.wait_for_group_runtime(
        session_id,
        "group force remove observed",
        lambda current: has_force_remove(current, graceful_members, force_member),
        timeout_seconds=group_poll_timeout,
        interval_seconds=group_poll_interval,
        require_inbox=False,
    )

    client.log("group turn 4: cancel and delete session")
    client.cancel_and_delete_session(session_id)
    client.log("Step C PASS — group inbox happy path")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_leader_args(parser)
    parser.add_argument("--group-poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--group-poll-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--group-min-members", type=int, default=2)
    args = parser.parse_args()

    try:
        run_group(
            api_base_url=args.api_base_url,
            token_file=args.token_file,
            group_poll_interval=args.group_poll_interval_seconds,
            group_poll_timeout=args.group_poll_timeout_seconds,
            group_min_members=args.group_min_members,
            http_timeout=args.http_timeout_seconds,
            llm_retries=args.llm_retries,
            llm_retry_delay=args.llm_retry_delay_seconds,
        )
        return 0
    except LlmError as exc:
        print(f"[biz-accept-C] FAIL (llm): {exc}", flush=True)
        return 1
    except NetworkError as exc:
        print(f"[biz-accept-C] FAIL (network): {exc}", flush=True)
        return 1
    except LeaderClientError as exc:
        print(f"[biz-accept-C] FAIL ({exc.category}): {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
