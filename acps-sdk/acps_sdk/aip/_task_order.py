"""Merge task results by the sender's state time, retaining terminal outcomes."""
from datetime import datetime, timezone

TERMINAL_STATES = frozenset({"completed", "failed", "rejected", "canceled", "cancelled"})


def result_time(result):
    value = result.get("status", {}).get("stateChangedAt") or result.get("sentAt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (AttributeError, TypeError, ValueError):
        return None


def accepts_task_update(candidate, previous):
    if previous is None:
        return True
    old_state = previous.get("status", {}).get("state")
    new_state = candidate.get("status", {}).get("state")
    if old_state in TERMINAL_STATES and new_state != old_state:
        return False
    before, after = result_time(previous), result_time(candidate)
    if before is not None:
        return after is not None and after >= before
    return True
