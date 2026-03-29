from __future__ import annotations

import argparse
import asyncio
import json
import socket
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent_service import build_agent_service
from .config import AppConfig, load_config
from .models import (
    Envelope,
    ErrorPayload,
    MessagePayload,
    SoloConfirmationPayload,
    SoloControlPayload,
    SoloStartPayload,
    SoloStatusPayload,
    SoloStepPayload,
    StatusPayload,
    utc_now,
)
from .providers.base import ReplyChunk, ReplyTrace
from .runtime_state import RuntimeState
from .solo_executor import SoloExecutor
from .solo_service import SoloService, SoloSessionState
from .solo_toolkit import SoloToolkit

app = FastAPI(title="openEagle Agent Backend")
config = load_config()
runtime_state = RuntimeState()
runtime_state.update_config(config)


def slog(message: str) -> None:
    print(f"[SOLO] {utc_now()} {message}", flush=True)


@app.on_event("startup")
async def announce_ready() -> None:
    port = getattr(app.state, "ws_port", None)
    if port is not None:
        print(f"[AGENT_READY] WS_PORT: {port}", flush=True)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def send_envelope(
    websocket: WebSocket,
    type_: str,
    request_id: str,
    conversation_id: str,
    payload: dict[str, Any],
) -> None:
    await websocket.send_text(
        json.dumps(
            {
                "type": type_,
                "requestId": request_id,
                "conversationId": conversation_id,
                "payload": payload,
                "timestamp": utc_now(),
            },
            ensure_ascii=False,
        )
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()
    active_solo: SoloSessionState | None = None
    solo_service: SoloService | None = None
    solo_executor = SoloExecutor()
    solo_tools = SoloToolkit(solo_executor)

    async def safe_send(
        type_: str,
        request_id: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> None:
        async with send_lock:
            await send_envelope(websocket, type_, request_id, conversation_id, payload)

    async def emit_solo_trace(
        session: SoloSessionState,
        name: str,
        status: str,
        summary: str,
        params: dict[str, Any] | None = None,
        result: Any | None = None,
    ) -> None:
        now = utc_now()
        await safe_send(
            "server:trace",
            session.request_id,
            session.conversation_id,
            {
                "trace": {
                    "id": f"solo-trace-{session.step_count}-{name}-{status}",
                    "kind": "skill",
                    "name": f"SOLO/{name}",
                    "status": "completed" if status != "error" else "error",
                    "summary": summary,
                    "params": params or {},
                    "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
                    "startedAt": now,
                    "completedAt": now,
                }
            },
        )

    async def emit_solo_status(session: SoloSessionState) -> None:
        payload = SoloStatusPayload(
            state=session.state,
            detail=session.detail,
            stepCount=session.step_count,
            maxSteps=session.max_steps,
            lastAction=session.last_action,
            startedAt=session.started_at,
            completedAt=session.completed_at,
            lastScreenshotAt=session.last_screenshot_at,
        ).model_dump(by_alias=True)
        await safe_send(
            "server:solo_status",
            session.request_id,
            session.conversation_id,
            {"status": payload},
        )

    async def emit_solo_step(
        session: SoloSessionState,
        step_index: int,
        action: str,
        action_args: dict[str, Any],
        thought_summary: str,
        expected_outcome: str,
        screenshot_path: str | None = None,
    ) -> None:
        payload = SoloStepPayload(
            stepIndex=step_index,
            action=action,
            actionArgs=action_args,
            thoughtSummary=thought_summary,
            expectedOutcome=expected_outcome,
            screenshotPath=screenshot_path,
            timestamp=utc_now(),
        ).model_dump(by_alias=True)
        await safe_send(
            "server:solo_step",
            session.request_id,
            session.conversation_id,
            {"step": payload},
        )

    async def emit_confirmation(
        session: SoloSessionState,
        step_index: int,
        reason: str,
        action: str,
        action_args: dict[str, Any],
        thought_summary: str,
    ) -> None:
        payload = SoloConfirmationPayload(
            stepIndex=step_index,
            reason=reason,
            action=action,
            actionArgs=action_args,
            thoughtSummary=thought_summary,
        ).model_dump(by_alias=True)
        await safe_send(
            "server:solo_confirmation_required",
            session.request_id,
            session.conversation_id,
            {"confirmation": payload},
        )

    async def process_step_result(
        session: SoloSessionState,
        result: dict[str, Any],
    ) -> None:
        success = bool(result.get("success", False))
        action = str(result.get("action", "unknown"))
        execution_error = result.get("executionError")
        screenshot = result.get("screenshot")
        screenshot_path: str | None = None
        screenshot_at: str | None = None
        if isinstance(screenshot, dict):
            path_value = screenshot.get("path")
            captured_at_value = screenshot.get("capturedAt")
            if isinstance(path_value, str):
                screenshot_path = path_value
            if isinstance(captured_at_value, str):
                screenshot_at = captured_at_value

        if screenshot_path:
            if screenshot_path == session.last_screenshot_path:
                session.same_screenshot_count += 1
            else:
                session.same_screenshot_count = 0
            session.last_screenshot_path = screenshot_path
        if screenshot_at:
            session.last_screenshot_at = screenshot_at

        if action == session.last_action:
            session.repeat_action_count += 1
        else:
            session.repeat_action_count = 1
        session.last_action = action

        if not success:
            session.state = "paused"
            session.detail = f"动作执行失败: {result.get('error', execution_error or 'unknown error')}"
            slog(f"request={session.request_id} step_result failed action={action} result={result}")
            await emit_solo_trace(
                session,
                "step_result",
                "error",
                f"动作执行失败: {action}",
                params={"action": action},
                result=result,
            )
            await emit_solo_status(session)
            return

        if execution_error:
            session.state = "paused"
            session.detail = f"动作执行异常: {execution_error}"
            slog(
                f"request={session.request_id} step_result execution_error action={action} error={execution_error}"
            )
            await emit_solo_trace(
                session,
                "step_result",
                "error",
                f"动作执行异常: {action}",
                params={"action": action},
                result=result,
            )
            await emit_solo_status(session)
            return

        session.step_count += 1
        session.detail = f"已完成第 {session.step_count} 步，准备下一步。"
        slog(
            f"request={session.request_id} step_result ok step={session.step_count} "
            f"action={action} execution_error={execution_error}"
        )
        await emit_solo_trace(
            session,
            "step_result",
            "completed",
            f"动作结果: {action}",
            params={"action": action, "step": session.step_count},
            result=result,
        )
        await emit_solo_status(session)

        if session.step_count >= session.max_steps:
            session.state = "paused"
            session.detail = f"超过最大步数 {session.max_steps}，已自动暂停。"
            await emit_solo_status(session)
            return

        if session.repeat_action_count >= 6:
            session.state = "paused"
            session.detail = "检测到连续重复动作（>=6 次），已自动暂停。"
            await emit_solo_status(session)
            return

        if session.same_screenshot_count >= 3:
            session.state = "paused"
            session.detail = "检测到连续截图无变化，已自动暂停。"
            await emit_solo_status(session)
            return

        if not session.last_screenshot_path:
            session.state = "paused"
            session.detail = "缺少新截图，无法继续。"
            await emit_solo_status(session)
            return

        await decide_and_emit_next_step(session, session.last_screenshot_path)

    async def execute_solo_step(
        session: SoloSessionState,
        action: str,
        action_args: dict[str, Any],
    ) -> None:
        try:
            execution_result = await asyncio.to_thread(
                solo_tools.execute,
                action,
                action_args,
            )
            screenshot = execution_result.get("screenshot")
            if not isinstance(screenshot, dict):
                screenshot = await asyncio.to_thread(solo_tools.screenshot)
            await process_step_result(
                session,
                {
                    "success": True,
                    "action": action,
                    "executionResult": execution_result,
                    "screenshot": screenshot,
                },
            )
        except Exception as exc:  # noqa: BLE001
            screenshot_payload: dict[str, Any] | None = None
            try:
                screenshot_payload = await asyncio.to_thread(solo_tools.screenshot)
            except Exception:  # noqa: BLE001
                screenshot_payload = None
            await process_step_result(
                session,
                {
                    "success": False,
                    "action": action,
                    "executionError": str(exc),
                    "screenshot": screenshot_payload,
                },
            )

    async def decide_and_emit_next_step(session: SoloSessionState, screenshot_path: str) -> None:
        nonlocal solo_service
        if solo_service is None:
            session.state = "error"
            session.detail = "SOLO 服务未初始化。"
            await emit_solo_status(session)
            return

        try:
            decision = await solo_service.decide_next(
                task=session.task,
                screenshot_path=screenshot_path,
                history=session.history,
            )
            slog(
                f"request={session.request_id} decision action={decision.action} "
                f"done={decision.is_task_done} step={session.step_count + 1}"
            )
            await emit_solo_trace(
                session,
                "decision",
                "completed",
                f"模型决策: {decision.action}",
                params={
                    "thought": decision.thought_summary,
                    "expected_outcome": decision.expected_outcome,
                },
                result=solo_service.decision_dict(decision),
            )
        except Exception as exc:  # noqa: BLE001
            session.state = "error"
            session.detail = f"VL 推理失败: {exc}"
            slog(f"request={session.request_id} decision error={exc}")
            await emit_solo_trace(
                session,
                "decision",
                "error",
                "VL 推理失败",
                result=str(exc),
            )
            await emit_solo_status(session)
            return

        session.history.append(
            {
                "step": session.step_count + 1,
                "decision": solo_service.decision_dict(decision),
                "timestamp": utc_now(),
            }
        )

        if decision.action == "finish" and session.step_count >= 2:
            session.state = "completed"
            session.completed_at = utc_now()
            session.detail = "SOLO 任务完成。"
            slog(f"request={session.request_id} completed at step={session.step_count}")
            await emit_solo_status(session)
            await safe_send(
                "server:message",
                session.request_id,
                session.conversation_id,
                {"content": "SOLO 任务已完成。"},
            )
            return
        if decision.action == "finish" and session.step_count < 2:
            slog(
                f"request={session.request_id} ignored early finish at step={session.step_count}"
            )
            await emit_solo_step(
                session,
                step_index=session.step_count + 1,
                action="wait",
                action_args={"ms": 600},
                thought_summary="模型过早结束，系统要求至少完成两步执行后再结束。",
                expected_outcome="等待后继续基于新截图决策",
                screenshot_path=screenshot_path,
            )
            return

        dangerous, reason = solo_service.is_dangerous_action(decision.action, decision.action_args)
        if dangerous:
            session.state = "waiting_user_confirmation"
            session.pending_confirmation = {
                "action": decision.action,
                "action_args": decision.action_args,
                "thought_summary": decision.thought_summary,
                "expected_outcome": decision.expected_outcome,
            }
            session.detail = "检测到危险动作，等待用户确认。"
            slog(
                f"request={session.request_id} waiting confirmation action={decision.action} reason={reason}"
            )
            await emit_solo_status(session)
            await emit_confirmation(
                session,
                step_index=session.step_count + 1,
                reason=reason,
                action=decision.action,
                action_args=decision.action_args,
                thought_summary=decision.thought_summary,
            )
            return

        await emit_solo_step(
            session,
            step_index=session.step_count + 1,
            action=decision.action,
            action_args=decision.action_args,
            thought_summary=decision.thought_summary,
            expected_outcome=decision.expected_outcome,
            screenshot_path=screenshot_path,
        )
        await execute_solo_step(session, decision.action, decision.action_args)

    try:
        while True:
            raw = await websocket.receive_text()
            envelope = Envelope.model_validate_json(raw)

            if envelope.type == "client:update_settings":
                next_config = AppConfig.model_validate(envelope.payload["settings"])
                runtime_state.update_config(next_config)
                solo_executor.set_preferred_display_index(
                    next_config.solo.preferred_display_index
                )
                await safe_send(
                    "server:status",
                    envelope.request_id,
                    envelope.conversation_id,
                    StatusPayload(stage="idle", detail="模型配置已同步").model_dump(),
                )
                continue

            if envelope.type == "client:list_solo_displays":
                current_config = runtime_state.get_config()
                solo_executor.set_preferred_display_index(
                    current_config.solo.preferred_display_index
                )
                displays = await asyncio.to_thread(solo_executor.list_displays, True)
                await safe_send(
                    "server:solo_displays",
                    envelope.request_id,
                    envelope.conversation_id,
                    {
                        "displays": displays,
                        "preferredDisplayIndex": current_config.solo.preferred_display_index,
                    },
                )
                continue

            if envelope.type == "client:start_solo":
                payload = SoloStartPayload.model_validate(envelope.payload)
                current_config = runtime_state.get_config()
                solo_service = SoloService(current_config.agent)
                solo_executor.set_preferred_display_index(
                    current_config.solo.preferred_display_index
                )
                first_screenshot = await asyncio.to_thread(solo_tools.screenshot)
                slog(
                    f"start request={envelope.request_id} conv={envelope.conversation_id} task={payload.content[:120]}"
                )
                active_solo = SoloSessionState(
                    request_id=envelope.request_id,
                    conversation_id=envelope.conversation_id,
                    task=payload.content,
                    started_at=utc_now(),
                    last_screenshot_path=str(first_screenshot.get("path", "")) or None,
                    last_screenshot_at=(
                        str(first_screenshot.get("capturedAt", ""))
                        if first_screenshot.get("capturedAt")
                        else None
                    ),
                    detail="SOLO 已启动，正在分析首帧截图。",
                )
                await emit_solo_status(active_solo)
                if not active_solo.last_screenshot_path:
                    active_solo.state = "error"
                    active_solo.detail = "首帧截图失败，无法启动 SOLO。"
                    await emit_solo_status(active_solo)
                else:
                    await decide_and_emit_next_step(active_solo, active_solo.last_screenshot_path)
                continue

            if envelope.type == "client:solo_control":
                control = SoloControlPayload.model_validate(envelope.payload)
                slog(
                    f"control request={envelope.request_id} action={control.action} "
                    f"solo_request={control.solo_request_id}"
                )
                if active_solo is None:
                    await safe_send(
                        "server:error",
                        envelope.request_id,
                        envelope.conversation_id,
                        ErrorPayload(message="当前没有进行中的 SOLO 任务", code="solo_missing").model_dump(),
                    )
                    continue

                if control.action == "pause":
                    active_solo.state = "paused"
                    active_solo.detail = "用户已暂停 SOLO。"
                    await emit_solo_trace(
                        active_solo,
                        "control",
                        "completed",
                        "用户暂停 SOLO",
                        params={"action": "pause"},
                    )
                    await emit_solo_status(active_solo)
                    continue

                if control.action == "resume":
                    if active_solo.last_screenshot_path is None:
                        active_solo.state = "error"
                        active_solo.detail = "缺少截图，无法恢复 SOLO。"
                        await emit_solo_status(active_solo)
                    else:
                        active_solo.state = "running"
                        active_solo.detail = "SOLO 已恢复。"
                        await emit_solo_trace(
                            active_solo,
                            "control",
                            "completed",
                            "用户恢复 SOLO",
                            params={"action": "resume"},
                        )
                        await emit_solo_status(active_solo)
                        await decide_and_emit_next_step(active_solo, active_solo.last_screenshot_path)
                    continue

                if control.action == "stop":
                    active_solo.state = "aborted"
                    active_solo.detail = "用户已结束 SOLO。"
                    active_solo.completed_at = utc_now()
                    await emit_solo_trace(
                        active_solo,
                        "control",
                        "completed",
                        "用户结束 SOLO",
                        params={"action": "stop"},
                    )
                    await emit_solo_status(active_solo)
                    continue

                if control.action == "confirm_allow":
                    pending = active_solo.pending_confirmation
                    if not pending:
                        await safe_send(
                            "server:error",
                            envelope.request_id,
                            envelope.conversation_id,
                            ErrorPayload(message="没有待确认动作", code="solo_no_pending_confirmation").model_dump(),
                        )
                        continue
                    active_solo.pending_confirmation = None
                    active_solo.state = "running"
                    active_solo.detail = "用户已允许危险动作，继续执行。"
                    await emit_solo_status(active_solo)
                    await emit_solo_step(
                        active_solo,
                        step_index=active_solo.step_count + 1,
                        action=pending["action"],
                        action_args=pending["action_args"],
                        thought_summary=pending["thought_summary"],
                        expected_outcome=pending["expected_outcome"],
                        screenshot_path=active_solo.last_screenshot_path,
                    )
                    await execute_solo_step(
                        active_solo,
                        str(pending["action"]),
                        dict(pending["action_args"]),
                    )
                    continue

                if control.action == "confirm_reject":
                    active_solo.pending_confirmation = None
                    active_solo.state = "paused"
                    active_solo.detail = "用户拒绝了危险动作，SOLO 已暂停。"
                    await emit_solo_status(active_solo)
                    continue

                if control.action == "step_result":
                    result = control.result or {}
                    await process_step_result(active_solo, result)
                    continue

                await safe_send(
                    "server:error",
                    envelope.request_id,
                    envelope.conversation_id,
                    ErrorPayload(message=f"Unsupported solo control action: {control.action}", code="solo_unsupported_control").model_dump(),
                )
                continue

            if envelope.type != "client:send_message":
                await safe_send(
                    "server:error",
                    envelope.request_id,
                    envelope.conversation_id,
                    ErrorPayload(
                        message="Unsupported message type",
                        code="unsupported_type",
                    ).model_dump(),
                )
                continue

            payload = MessagePayload.model_validate(envelope.payload)

            await safe_send(
                "server:status",
                envelope.request_id,
                envelope.conversation_id,
                StatusPayload(
                    stage="thinking",
                    detail="后端正在生成回复",
                ).model_dump(),
            )

            agent_service = build_agent_service(runtime_state.get_config())
            chunks: list[str] = []
            async for event in agent_service.stream_reply(
                envelope.conversation_id,
                payload.content,
            ):
                if isinstance(event, ReplyChunk):
                    chunks.append(event.content)
                    await safe_send(
                        "server:message_delta",
                        envelope.request_id,
                        envelope.conversation_id,
                        {"content": event.content},
                    )
                    continue

                if isinstance(event, ReplyTrace):
                    await safe_send(
                        "server:trace",
                        envelope.request_id,
                        envelope.conversation_id,
                        {
                            "trace": {
                                "id": event.trace_id,
                                "kind": event.kind,
                                "name": event.name,
                                "status": event.status,
                                "summary": event.summary,
                                "params": event.params,
                                "result": event.result,
                                "startedAt": event.started_at,
                                "completedAt": event.completed_at,
                            }
                        },
                    )

            reply = "".join(chunks)

            await safe_send(
                "server:message",
                envelope.request_id,
                envelope.conversation_id,
                {"content": reply},
            )

            await safe_send(
                "server:status",
                envelope.request_id,
                envelope.conversation_id,
                StatusPayload(stage="idle", detail="回复完成").model_dump(),
            )
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        await safe_send(
            "server:error",
            "server-error",
            "unknown",
            ErrorPayload(message=str(exc), code="internal_error").model_dump(),
        )


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


async def serve(host: str, port: int) -> None:
    actual_port = port if port != 0 else find_free_port(host)
    app.state.ws_port = actual_port
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=actual_port,
            log_level="info",
            ws="websockets",
        )
    )
    await server.serve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()
