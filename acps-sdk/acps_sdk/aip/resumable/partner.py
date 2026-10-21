"""Opt-in command-handler composition for fenced modifying task payloads."""
from __future__ import annotations

import json
from typing import Any, Callable

from .ledger import Fence, OperationLedger, OperationOutcome


class ResumableGroupPartner:
    def __init__(self, client: Any, ledger: OperationLedger):
        self.client, self.ledger = client, ledger

    def set_command_handler(self, handler: Callable[..., Any], *,
                            payload_extractor: Callable[[Any], dict[str, Any]] | None = None,
                            result_handler: Callable[[Any, OperationOutcome], Any] | None = None,
                            inspector: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Wrap write/high tasks; reads retain their normal command callback.

        The payload risk is policy output, not a trust boundary. Gateways must
        classify tools before entering this adapter. Result publication remains
        explicit through result_handler, using the original AIP client methods.
        """
        async def wrapped(command: Any, is_mentioned: bool) -> Any:
            if not is_mentioned:
                return None
            payload = payload_extractor(command) if payload_extractor else self._payload(command)
            if payload.get("risk", payload.get("risk_level", "read")) not in ("write", "high"):
                return await handler(command, is_mentioned)
            if not payload.get("operation_id") or not payload.get("fence"):
                outcome = OperationOutcome(str(payload.get("operation_id", "")), "rejected_missing_operation_identity")
            else:
                outcome = await self.ledger.execute(payload["operation_id"], payload.get("parameters", payload.get("args", {})),
                    Fence(**payload["fence"]), lambda: handler(command, is_mentioned),
                    inspect=(lambda: inspector(payload)) if inspector else None,
                    expected_before=payload.get("expected_before"), expected_after=payload.get("expected_after"))
            if result_handler:
                await result_handler(command, outcome)
            return outcome
        self.client.set_command_handler(wrapped)

    @staticmethod
    def _payload(command: Any) -> dict[str, Any]:
        for item in command.dataItems:
            text = getattr(item, "text", None)
            if text:
                try:
                    value = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    return value
            data = getattr(item, "data", None)
            if isinstance(data, dict):
                return data
        return {}

