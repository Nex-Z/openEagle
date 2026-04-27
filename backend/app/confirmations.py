from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from .models import utc_now

ToolDecision = Literal["allow", "reject"]
CONFIRMATION_TTL_SECONDS = 300


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
    expires_at: float = field(default_factory=lambda: time.time() + CONFIRMATION_TTL_SECONDS)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

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

    def _cleanup_expired(self) -> None:
        expired_ids = [cid for cid, p in self._pending.items() if p.is_expired()]
        for cid in expired_ids:
            del self._pending[cid]

    def create(
        self,
        request_id: str,
        conversation_id: str,
        kind: str,
        name: str,
        reason: str,
        params: dict[str, Any],
    ) -> PendingToolConfirmation:
        self._cleanup_expired()
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
        pending = self._pending.pop(confirmation_id, None)
        if pending and pending.is_expired():
            return None
        return pending

    def get(self, confirmation_id: str) -> PendingToolConfirmation | None:
        pending = self._pending.get(confirmation_id)
        if pending and pending.is_expired():
            del self._pending[confirmation_id]
            return None
        return pending
