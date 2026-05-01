from __future__ import annotations

from collections.abc import Awaitable, Callable


StartSolo = Callable[[str, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]


class SoloWorkerAdapter:
    """Thin adapter that lets AgentRuntime treat SOLO as a worker.

    The adapter intentionally does not inspect or steer SOLO's internal loop.
    SOLO remains responsible for visual observation, execution, recovery, and
    validation; the main runtime only starts and controls the session.
    """

    def __init__(self, start_solo: StartSolo, solo_control: SoloControl) -> None:
        self._start_solo = start_solo
        self._solo_control = solo_control

    async def start(self, conversation_id: str, request_id: str, task: str) -> str:
        return await self._start_solo(conversation_id, task, request_id)

    async def pause(self, conversation_id: str, request_id: str) -> str:
        return await self._solo_control(conversation_id, request_id, "pause")

    async def resume(self, conversation_id: str, request_id: str) -> str:
        return await self._solo_control(conversation_id, request_id, "resume")

    async def stop(self, conversation_id: str, request_id: str) -> str:
        return await self._solo_control(conversation_id, request_id, "stop")

    async def confirm(self, conversation_id: str, request_id: str, decision: str) -> str:
        action = "confirm_allow" if decision == "allow" else "confirm_reject"
        return await self._solo_control(conversation_id, request_id, action)

    async def get_report(self, conversation_id: str, request_id: str) -> str:
        _ = (conversation_id, request_id)
        return "SOLO 的运行报告通过现有 status、step、plan 和 final message 事件持续产出。"
