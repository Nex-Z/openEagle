from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from .models import utc_now

ToolDecision = Literal["allow", "reject"]


@dataclass
class PendingToolConfirmation:
    confirmation_id: str
    request_id: str
    conversation_id: str
    kind: str
    name: str
    reason: str
    params: dict[str, Any]
    created_at: str = field(default_factory=utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "confirmationId": self.confirmation_id,
            "riskLevel": "confirm",
            "kind": self.kind,
            "name": self.name,
            "reason": self.reason,
            "params": self.params,
            "createdAt": self.created_at,
        }


class ToolConfirmationStore:
    def __init__(self) -> None:
        self._pending: dict[str, PendingToolConfirmation] = {}

    def create(
        self,
        request_id: str,
        conversation_id: str,
        kind: str,
        name: str,
        reason: str,
        params: dict[str, Any],
    ) -> PendingToolConfirmation:
        pending = PendingToolConfirmation(
            confirmation_id=f"confirm-{uuid4()}",
            request_id=request_id,
            conversation_id=conversation_id,
            kind=kind,
            name=name,
            reason=reason,
            params=params,
        )
        self._pending[pending.confirmation_id] = pending
        return pending

    def pop(self, confirmation_id: str) -> PendingToolConfirmation | None:
        return self._pending.pop(confirmation_id, None)

    def get(self, confirmation_id: str) -> PendingToolConfirmation | None:
        return self._pending.get(confirmation_id)
