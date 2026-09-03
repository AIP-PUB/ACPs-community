#!/usr/bin/env python3
"""业务验收步骤 B — Leader↔Partner direct_rpc。

Happy path（与遗留 smoke/business.py direct_rpc 对齐）：
  greeting → travel task →（仅当仍有 AwaitingInput 时）supplement → cancel/end。

硬性规则：
  - 仅有限 LLM 重试；LLM 错误不得跳过成功。
  - Auth：OIDC 开启时用 Bearer token 文件；local auth 无 token。
  - turn 2 已收敛且无 AwaitingInput 时 WARNING 跳过 supplement（不 Fail）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/business_acceptance_step_b.py` without PYTHONPATH.
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
)


def run_direct_rpc(
    *,
    api_base_url: str,
    token_file: str,
    rpc_poll_interval: float,
    rpc_poll_timeout: float,
    task_poll_timeout: float,
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
    client = build_client_from_args(args, log_prefix="biz-accept-B")

    client.log("direct_rpc turn 1: greeting")
    session_id, _ = client.submit(query=GREETING_QUERY, mode="direct_rpc")
    snapshot = client.poll_result(session_id, "direct_rpc", rpc_poll_timeout, rpc_poll_interval)
    client.ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"direct_rpc turn 1 did not converge: {snapshot}",
    )
    client.ensure(snapshot["dialog_turns"] >= 1, f"direct_rpc turn 1 dialog turns: {snapshot}")
    if snapshot["partner_task_count"] >= 1:
        client.log("turn 1 already entered partner clarification; continuing happy path")

    client.log("direct_rpc turn 2: create travel task")
    session_id_again, _ = client.submit(
        query=TRAVEL_PLAN_QUERY,
        mode="direct_rpc",
        session_id=session_id,
        active_task_id=snapshot["active_task_id"],
    )
    client.ensure(session_id_again == session_id, "direct_rpc turn 2 sessionId changed")
    snapshot = client.poll_result(session_id, "direct_rpc", task_poll_timeout, rpc_poll_interval)
    client.ensure(
        snapshot["result_type"] in {"final", "clarification"},
        f"direct_rpc turn 2 did not converge: {snapshot}",
    )
    client.ensure(snapshot["dialog_turns"] >= 2, f"direct_rpc turn 2 dialog turns: {snapshot}")
    client.ensure(
        snapshot["partner_task_count"] >= 1,
        f"direct_rpc turn 2 missing partnerTasks (Partner participation not visible): {snapshot}",
    )
    client.log(f"partnerTasks visible: count={snapshot['partner_task_count']}")
    client.log(
        "direct_rpc turn 2 state: "
        f"result_type={snapshot.get('result_type')} "
        f"awaiting_input={snapshot.get('awaiting_input_count')} "
        f"partner_states={snapshot.get('partner_states')}"
    )

    if LeaderApiClient.needs_task_input_supplement(snapshot):
        client.log("direct_rpc turn 3: supplement / clarification")
        session_id_again, _ = client.submit(
            query=TRAVEL_SUPPLEMENT_QUERY,
            mode="direct_rpc",
            session_id=session_id,
            active_task_id=snapshot["active_task_id"],
        )
        client.ensure(session_id_again == session_id, "direct_rpc turn 3 sessionId changed")
        snapshot = client.poll_result(session_id, "direct_rpc", task_poll_timeout, rpc_poll_interval)
        client.ensure(
            snapshot["result_type"] in {"final", "clarification"},
            f"direct_rpc turn 3 did not converge: {snapshot}",
        )
        client.ensure(snapshot["dialog_turns"] >= 3, f"direct_rpc turn 3 dialog turns: {snapshot}")
    else:
        client.log(
            f"{WARNING_SKIP_SUPPLEMENT_PREFIX} "
            f"(result_type={snapshot.get('result_type')}, "
            f"states={snapshot.get('partner_states')}); continue lifecycle"
        )

    client.log("direct_rpc turn 4: cancel session")
    client.cancel_session(session_id)
    client.log("Step B PASS — direct_rpc happy path")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_leader_args(parser)
    parser.add_argument("--rpc-poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--rpc-poll-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--task-poll-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()

    try:
        run_direct_rpc(
            api_base_url=args.api_base_url,
            token_file=args.token_file,
            rpc_poll_interval=args.rpc_poll_interval_seconds,
            rpc_poll_timeout=args.rpc_poll_timeout_seconds,
            task_poll_timeout=args.task_poll_timeout_seconds,
            http_timeout=args.http_timeout_seconds,
            llm_retries=args.llm_retries,
            llm_retry_delay=args.llm_retry_delay_seconds,
        )
    except LlmError as exc:
        print(f"[biz-accept-B] FAIL (llm): {exc}", file=sys.stderr)
        return 2
    except NetworkError as exc:
        print(f"[biz-accept-B] FAIL (network): {exc}", file=sys.stderr)
        return 2
    except LeaderClientError as exc:
        print(f"[biz-accept-B] FAIL ({exc.category}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
