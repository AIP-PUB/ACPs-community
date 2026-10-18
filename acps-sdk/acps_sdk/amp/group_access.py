"""One AMP access record for the first Start of each addressed task attempt.

MessageEmitter remains the official transport-edge instrumentation. Access is
an independent operation record and uses sessionId / taskId for reconciliation.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from time import monotonic
from typing import Any, Callable

from .access_emitter import AccessEmitter
from .models import AccessBody, AccessParticipant, AccessRequest, AccessResponse, ErrorInfo

logger = logging.getLogger(__name__)


class GroupAccessMiddleware:
    def __init__(self, emitter: AccessEmitter, aic: str, *, route_resolver: Callable[[Any], str] | None = None,
                 seen_capacity: int = 8192):
        if type(seen_capacity) is not int or seen_capacity < 1:
            raise ValueError("seen_capacity must be a positive integer")
        self.emitter, self.aic = emitter, aic
        self.route_resolver = route_resolver
        self.seen_capacity = seen_capacity
        self._seen: OrderedDict[tuple[str | None, str | None, str], None] = OrderedDict()

    def route(self, command: Any) -> str:
        if self.route_resolver:
            return self.route_resolver(command)
        params = getattr(command, "commandParams", None) or {}
        for item in getattr(command, "dataItems", None) or []:
            value = getattr(item, "data", None)
            if isinstance(value, dict):
                params = {**value, **params}
                break
            text = getattr(item, "text", None)
            if text:
                try:
                    value = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    params = {**value, **params}
                    break
        return str(params.get("skill") or params.get("skill_id") or params.get("tool") or "aip.group.task")

    async def run(self, command: Any, callback: Callable[..., Any], *, direction: str = "receive") -> Any:
        method = getattr(command.command, "value", str(command.command))
        key = (getattr(command, "sessionId", None), getattr(command, "taskId", None), direction)
        if method != "start":
            return await callback()
        if key in self._seen:
            self._seen.move_to_end(key)
            return await callback()
        # A handler failure is the first attempt's outcome. Redelivering its
        # same TaskId still invokes the handler but does not add another access.
        self._seen[key] = None
        if len(self._seen) > self.seen_capacity:
            self._seen.popitem(last=False)
        start = monotonic()
        error = None
        result = None
        try:
            result = await callback()
            status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
            if isinstance(status, str) and status not in ("ok", "applied", "skipped_already_applied", "reconciled_applied"):
                error = str(status)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            mentions = getattr(command, "mentions", None)
            callee = self.aic if direction == "receive" else (
                mentions[0] if isinstance(mentions, list) and len(mentions) == 1 else "group:" + str(getattr(command, "groupId", "")))
            body = AccessBody(
                duration_ms=(monotonic() - start) * 1000,
                request=AccessRequest(method=method, route=self.route(command)),
                response=AccessResponse(status_code=500 if error else (202 if direction == "send" else 200)),
                caller=AccessParticipant(aic=getattr(command, "senderId", None)),
                callee=AccessParticipant(aic=callee),
                error=ErrorInfo(code=error, message=error) if error else None,
            )
            try:
                await self.emitter.emit(body, trace_id=getattr(command, "sessionId", None),
                                        correlation_id=getattr(command, "taskId", None))
            except Exception:
                logger.warning("Group access emission failed", exc_info=True)

