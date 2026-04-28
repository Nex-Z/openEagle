from __future__ import annotations

import argparse
import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent_service import build_agent_service
from .config import AppConfig, load_config
from .confirmations import ToolConfirmationStore
from .default_tools import execute_confirmed_tool
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
    ToolConfirmationPayload,
    utc_now,
)
from .providers.base import ReplyChunk, ReplyToolConfirmation, ReplyTrace
from .runtime_state import RuntimeState
from .safety import assess_solo_action
from .solo_executor import SoloExecutor
from .solo_run_logger import SoloRunLogger
from .solo_service import SoloService, SoloSessionState, summarize_solo_step_result
from .solo_toolkit import SoloToolkit

app = FastAPI(title="openEagle Agent Backend")
config = load_config()
runtime_state = RuntimeState()
runtime_state.update_config(config)
workspace_root = Path(__file__).resolve().parents[2]
confirmed_tool_results: dict[str, str] = {}


def slog(message: str) -> None:
    print(f"[SOLO] {utc_now()} {message}", flush=True)


_APP_KEYWORDS: dict[str, list[str]] = {
    "VS Code": ["vscode", "vs code", "visual studio code", "代码编辑器"],
    "Chrome": ["chrome", "谷歌浏览器", "网页", "浏览器"],
    "Edge": ["edge", "微软浏览器"],
    "Firefox": ["firefox", "火狐"],
    "终端": ["终端", "terminal", "cmd", "powershell", "命令行"],
    "资源管理器": ["资源管理器", "文件管理器", "explorer", "finder"],
    "Excel": ["excel", "表格", "电子表格"],
    "Word": ["word", "文档", "文字处理"],
    "PPT": ["ppt", "powerpoint", "演示文稿"],
    "微信": ["微信", "wechat"],
    "钉钉": ["钉钉", "dingtalk"],
}


def _infer_app_context(task: str) -> str | None:
    task_lower = task.lower()
    detected = [app for app, keywords in _APP_KEYWORDS.items() if any(kw in task_lower for kw in keywords)]
    if detected:
        return f"任务涉及以下应用: {', '.join(detected)}。"
    return None


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
    active_solo_task: asyncio.Task[None] | None = None
    solo_service: SoloService | None = None
    solo_executor = SoloExecutor()
    solo_tools = SoloToolkit(solo_executor)
    solo_logger = SoloRunLogger(workspace_root)
    tool_confirmations = ToolConfirmationStore()

    async def safe_send(
        type_: str,
        request_id: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> None:
        async with send_lock:
            await send_envelope(websocket, type_, request_id, conversation_id, payload)

    def is_solo_running(session: SoloSessionState) -> bool:
        return active_solo is session and session.state == "running"

    async def handle_solo_task_error(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            if active_solo is None or active_solo.state in {"aborted", "completed", "error"}:
                return
            active_solo.state = "error"
            active_solo.detail = f"SOLO 后台任务异常: {exc}"
            solo_logger.write("error", {"reason": active_solo.detail})
            await emit_solo_status(active_solo)

    def schedule_solo_task(coro: Any) -> None:
        nonlocal active_solo_task
        if active_solo_task and not active_solo_task.done():
            active_solo_task.cancel()
        task = asyncio.create_task(coro)
        active_solo_task = task

        def on_done(done: asyncio.Task[None]) -> None:
            nonlocal active_solo_task
            if active_solo_task is done:
                active_solo_task = None
            asyncio.create_task(handle_solo_task_error(done))

        task.add_done_callback(on_done)

    def cancel_active_solo_task() -> None:
        nonlocal active_solo_task
        if active_solo_task and not active_solo_task.done():
            active_solo_task.cancel()
        active_solo_task = None

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
            logPath=session.log_path,
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
        agent_message: str | None = None,
        screenshot_path: str | None = None,
    ) -> None:
        payload = SoloStepPayload(
            stepIndex=step_index,
            action=action,
            actionArgs=action_args,
            thoughtSummary=thought_summary,
            agentMessage=agent_message,
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


    def _build_final_report(session: SoloSessionState, decision: "SoloDecision | None" = None) -> str:
        lines: list[str] = []

        if decision and decision.agent_message:
            lines.append(decision.agent_message)
        elif session.last_agent_message:
            lines.append(session.last_agent_message)

        # If the agent didn't integrate findings into its message, append them
        if session.findings:
            agent_msg = (decision.agent_message if decision else "") or session.last_agent_message or ""
            findings_mentioned = all(f in agent_msg for f in session.findings[-3:])
            if not findings_mentioned:
                lines.append("")
                for finding in session.findings:
                    lines.append(f"- {finding}")

        return "\n".join(lines) if lines else "抱歉，任务执行过程中出现了问题，未能完成。"

    async def execute_solo_step(
        session: SoloSessionState,
        action: str,
        action_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a single SOLO action and return the execution result dict.

        Returns a dict with keys: success, action, executionResult, screenshot.
        On exception returns success=False with executionError.
        Does NOT update session.step_count or loop — caller is responsible.
        """
        if not is_solo_running(session):
            return {"success": False, "action": action, "executionError": "session not running"}

        try:
            execution_result = await asyncio.to_thread(
                solo_tools.execute,
                action,
                action_args,
            )
            if not is_solo_running(session):
                return {"success": False, "action": action, "executionError": "session stopped"}
            screenshot = execution_result.get("screenshot")
            if not isinstance(screenshot, dict):
                screenshot = await asyncio.to_thread(solo_tools.screenshot)
            return {"success": True, "action": action, "executionResult": execution_result, "screenshot": screenshot}
        except Exception as exc:  # noqa: BLE001
            if not is_solo_running(session):
                return {"success": False, "action": action, "executionError": "session stopped"}
            screenshot_payload: dict[str, Any] | None = None
            try:
                screenshot_payload = await asyncio.to_thread(solo_tools.screenshot)
            except Exception:  # noqa: BLE001
                screenshot_payload = None
            return {"success": False, "action": action, "executionError": str(exc), "screenshot": screenshot_payload}

    async def agent_loop(
        session: SoloSessionState,
        screenshot_path: str,
    ) -> None:
        """Core observe→think→act loop. Replaces execute_plan_step + replan_and_continue + decide_and_emit_next_step."""
        nonlocal solo_service
        if solo_service is None:
            session.state = "error"
            session.detail = "SOLO 服务未初始化。"
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_status(session)
            return

        while is_solo_running(session):
            # ━━ STEP 1: VL reasons and decides ━━
            try:
                app_context = _infer_app_context(session.task)
                decision = await solo_service.decide_next(
                    task=session.task,
                    screenshot_path=screenshot_path,
                    history=session.history,
                    display_index=session.display_index,
                    app_context=app_context,
                    findings=session.findings,
                )
            except Exception as exc:  # noqa: BLE001
                if not is_solo_running(session):
                    return
                session.state = "error"
                session.detail = f"VL 推理失败: {exc}"
                slog(f"request={session.request_id} decision error={exc}")
                solo_logger.write("error", {"reason": session.detail})
                await emit_solo_trace(
                    session, "decision", "error", "VL 推理失败", result=str(exc),
                )
                await emit_solo_status(session)
                return

            if not is_solo_running(session):
                return

            if decision.agent_message:
                session.last_agent_message = decision.agent_message

            # Accumulate findings
            if decision.findings:
                session.findings.extend(decision.findings)

            slog(
                f"request={session.request_id} decision action={decision.action} "
                f"done={decision.is_task_done} step={session.step_count + 1}"
            )

            # ━━ STEP 2: Check completion ━━
            # Guard: reject is_task_done if the agent has collected zero information.
            #
            # is_task_done is a passive signal — the model says "I think I'm done" while
            # also picking a next action. If findings is empty, the agent hasn't actually
            # extracted any information for the user, which means it has nothing to report.
            # Tasks that don't need findings (e.g. "open notepad") should use action="finish".
            #
            # action="finish" is ALWAYS honored — the model explicitly chose to end.
            premature_finish = False
            if decision.is_task_done and decision.action != "finish":
                if not session.findings:
                    premature_finish = True
                    slog(
                        f"request={session.request_id} rejecting is_task_done "
                        f"with empty findings step={session.step_count} last_action={session.last_action}"
                    )

            if (decision.is_task_done or decision.action == "finish") and not premature_finish:
                report = _build_final_report(session, decision)
                session.state = "completed"
                session.completed_at = utc_now()
                session.detail = "SOLO 任务完成。"
                slog(f"request={session.request_id} completed at step={session.step_count}")
                solo_logger.write("completed", {"step": session.step_count, "source": "agent_loop"})
                await emit_solo_status(session)
                await safe_send(
                    "server:message",
                    session.request_id,
                    session.conversation_id,
                    {"content": f"**SOLO 任务已完成。**\n\n{report}"},
                )
                return

            # If premature finish was rejected, override with a screenshot to get fresh state
            if premature_finish:
                decision = solo_service._fallback_decision_from_text(
                    "premature finish rejected", ValueError("premature is_task_done after passive action")
                )
                # decision.action is "screenshot" — will be executed normally below

            # ━━ STEP 3: Safety checks ━━
            assessment = assess_solo_action(decision.action, decision.action_args, workspace_root)

            if assessment.level == "blocked":
                session.state = "paused"
                session.detail = f"动作已阻断: {assessment.reason}"
                solo_logger.write(
                    "paused",
                    {"reason": session.detail, "action": decision.action, "actionArgs": decision.action_args},
                )
                await emit_solo_status(session)
                return

            permission_mode = runtime_state.get_config().permissions.mode
            if assessment.level == "confirm" and permission_mode != "all":
                session.state = "waiting_user_confirmation"
                session.pending_confirmation = {
                    "action": decision.action,
                    "action_args": decision.action_args,
                    "thought_summary": decision.thought_summary,
                    "expected_outcome": decision.progress or decision.expected_outcome,
                    "agent_message": decision.agent_message,
                    "risk_level": assessment.level,
                    "reason": assessment.reason,
                }
                session.detail = "检测到危险动作，等待用户确认。"
                slog(
                    f"request={session.request_id} waiting confirmation "
                    f"action={decision.action} reason={assessment.reason}"
                )
                solo_logger.write(
                    "confirmation_required",
                    {
                        "step": session.step_count + 1,
                        "action": decision.action,
                        "actionArgs": decision.action_args,
                        "reason": assessment.reason,
                    },
                )
                await emit_solo_status(session)
                await emit_confirmation(
                    session,
                    step_index=session.step_count + 1,
                    reason=assessment.reason,
                    action=decision.action,
                    action_args=decision.action_args,
                    thought_summary=decision.thought_summary,
                )
                return  # Wait for user; resume will re-enter agent_loop

            # ━━ STEP 4: Emit decision trace + step to frontend ━━
            await emit_solo_trace(
                session,
                "decision",
                "completed",
                f"视觉决策: {decision.action}",
                params={
                    "thought": decision.thought_summary,
                    "expected_outcome": decision.progress or decision.expected_outcome,
                },
                result=solo_service.decision_dict(decision),
            )
            solo_logger.write(
                "decision",
                {
                    "step": session.step_count + 1,
                    "decision": solo_service.decision_dict(decision),
                    "screenshotPath": screenshot_path,
                    "modelElapsedMs": decision.model_elapsed_ms,
                    "repairElapsedMs": decision.repair_elapsed_ms,
                    "imageBytes": decision.image_bytes,
                    "sourceImageBytes": decision.source_image_bytes,
                    "modelImagePath": decision.model_image_path,
                    "modelImageWidth": decision.model_image_width,
                    "modelImageHeight": decision.model_image_height,
                    "modelImageScale": decision.model_image_scale,
                },
            )
            if decision.raw_model_output:
                solo_logger.write(
                    "decision_parse_recovery",
                    {
                        "step": session.step_count + 1,
                        "usedFallback": decision.used_parse_fallback,
                        "rawOutput": decision.raw_model_output,
                        "repairOutput": decision.repair_model_output,
                    },
                )
                await emit_solo_trace(
                    session,
                    "decision_repair",
                    "completed",
                    (
                        "VL 输出不是标准 JSON，已保留自然语言并生成保守动作。"
                        if decision.used_parse_fallback
                        else "VL 输出不是标准 JSON，已修复为动作决策。"
                    ),
                    params={
                        "usedFallback": decision.used_parse_fallback,
                        "modelElapsedMs": decision.model_elapsed_ms,
                        "repairElapsedMs": decision.repair_elapsed_ms,
                        "imageBytes": decision.image_bytes,
                        "sourceImageBytes": decision.source_image_bytes,
                        "modelImageWidth": decision.model_image_width,
                        "modelImageHeight": decision.model_image_height,
                        "modelImageScale": decision.model_image_scale,
                    },
                )

            session.history.append(
                {
                    "step": session.step_count + 1,
                    "decision": solo_service.decision_dict(decision),
                    "timestamp": utc_now(),
                }
            )

            await emit_solo_step(
                session,
                step_index=session.step_count + 1,
                action=decision.action,
                action_args=decision.action_args,
                thought_summary=decision.thought_summary,
                expected_outcome=decision.progress or decision.expected_outcome,
                agent_message=decision.agent_message,
                screenshot_path=screenshot_path,
            )

            # ━━ STEP 5: Execute action ━━
            try:
                execution_result = await asyncio.to_thread(
                    solo_tools.execute,
                    decision.action,
                    decision.action_args,
                )
                if not is_solo_running(session):
                    return

                new_screenshot = execution_result.get("screenshot")
                if not isinstance(new_screenshot, dict):
                    new_screenshot = await asyncio.to_thread(solo_tools.screenshot)
                if not is_solo_running(session):
                    return

                # Update session state
                session.step_count += 1
                if decision.action == session.last_action:
                    session.repeat_action_count += 1
                else:
                    session.repeat_action_count = 1
                session.last_action = decision.action

                # Update screenshot tracking
                if isinstance(new_screenshot, dict):
                    new_path = new_screenshot.get("path")
                    new_hash = new_screenshot.get("contentHash")
                    if isinstance(new_path, str):
                        session.last_screenshot_path = new_path
                        screenshot_path = new_path
                    if isinstance(new_hash, str):
                        if new_hash == session.last_screenshot_hash:
                            session.same_screenshot_count += 1
                        else:
                            session.same_screenshot_count = 0
                        session.last_screenshot_hash = new_hash

                # Record result
                result_summary = summarize_solo_step_result(
                    {"success": True, "action": decision.action, "executionResult": execution_result, "screenshot": new_screenshot}
                )
                session.history[-1]["result"] = result_summary
                solo_logger.write(
                    "action_result",
                    {"step": session.step_count, "action": decision.action, "result": result_summary},
                )
                await emit_solo_trace(
                    session,
                    "step_result",
                    "completed",
                    f"视觉动作结果: {decision.action}",
                    params={"action": decision.action, "step": session.step_count},
                    result=result_summary,
                )
                await emit_solo_status(session)

            except Exception as exc:  # noqa: BLE001
                if not is_solo_running(session):
                    return
                session.step_count += 1
                session.history[-1]["result"] = {"success": False, "error": str(exc)}
                session.state = "paused"
                session.detail = f"动作执行失败: {exc}"
                slog(f"request={session.request_id} action error={exc}")
                solo_logger.write("error", {"reason": session.detail, "action": decision.action})
                await emit_solo_status(session)
                return

            # ━━ STEP 6: Safety rails ━━
            if session.step_count >= session.max_steps:
                session.state = "paused"
                session.detail = f"超过最大步数 {session.max_steps}，已自动暂停。"
                solo_logger.write("paused", {"reason": session.detail})
                await emit_solo_status(session)
                return

            if session.repeat_action_count >= 4:
                session.state = "paused"
                session.detail = "检测到连续重复动作（>=4 次），已自动暂停。"
                solo_logger.write("paused", {"reason": session.detail})
                await emit_solo_status(session)
                return

            if session.same_screenshot_count >= 3:
                session.state = "paused"
                session.detail = "检测到连续截图无变化（>=3 次），已自动暂停。"
                solo_logger.write("paused", {"reason": session.detail})
                await emit_solo_status(session)
                return

            # ━━ STEP 7: Loop continues naturally (observe → think → act) ━━

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

            if envelope.type == "client:tool_confirmation":
                control = ToolConfirmationPayload.model_validate(envelope.payload)
                pending = tool_confirmations.pop(control.confirmation_id)
                if pending is None:
                    await safe_send(
                        "server:error",
                        envelope.request_id,
                        envelope.conversation_id,
                        ErrorPayload(
                            message="没有找到待确认工具动作",
                            code="tool_confirmation_missing",
                        ).model_dump(),
                    )
                    continue

                if control.decision != "allow":
                    await safe_send(
                        "server:message",
                        pending.request_id,
                        pending.conversation_id,
                        {"content": f"已拒绝执行工具动作：{pending.name}。"},
                    )
                    continue

                now = utc_now()
                await safe_send(
                    "server:trace",
                    pending.request_id,
                    pending.conversation_id,
                    {
                        "trace": {
                            "id": f"confirmed-{pending.confirmation_id}",
                            "kind": "tool",
                            "name": pending.name,
                            "status": "started",
                            "summary": "用户已确认，开始执行工具动作。",
                            "params": pending.params,
                            "startedAt": now,
                        }
                    },
                )
                try:
                    result = await asyncio.to_thread(
                        execute_confirmed_tool,
                        workspace_root,
                        pending,
                    )
                    confirmed_tool_results[pending.conversation_id] = (
                        f"上一轮确认执行的工具 `{pending.name}` 结果：\n{result}"
                    )
                    completed_at = utc_now()
                    await safe_send(
                        "server:trace",
                        pending.request_id,
                        pending.conversation_id,
                        {
                            "trace": {
                                "id": f"confirmed-{pending.confirmation_id}",
                                "kind": "tool",
                                "name": pending.name,
                                "status": "completed",
                                "summary": "用户确认后的工具动作已完成。",
                                "params": pending.params,
                                "result": result,
                                "startedAt": now,
                                "completedAt": completed_at,
                            }
                        },
                    )
                    await safe_send(
                        "server:message",
                        pending.request_id,
                        pending.conversation_id,
                        {"content": f"已执行确认动作 `{pending.name}`。\n\n```text\n{result}\n```"},
                    )
                except Exception as exc:  # noqa: BLE001
                    completed_at = utc_now()
                    await safe_send(
                        "server:trace",
                        pending.request_id,
                        pending.conversation_id,
                        {
                            "trace": {
                                "id": f"confirmed-{pending.confirmation_id}",
                                "kind": "tool",
                                "name": pending.name,
                                "status": "error",
                                "summary": "用户确认后的工具动作执行失败。",
                                "params": pending.params,
                                "result": str(exc),
                                "startedAt": now,
                                "completedAt": completed_at,
                            }
                        },
                    )
                    await safe_send(
                        "server:message",
                        pending.request_id,
                        pending.conversation_id,
                        {"content": f"确认后的工具动作 `{pending.name}` 执行失败：{exc}"},
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
                    last_screenshot_hash=(
                        str(first_screenshot.get("contentHash", ""))
                        if first_screenshot.get("contentHash")
                        else None
                    ),
                    last_screenshot_at=(
                        str(first_screenshot.get("capturedAt", ""))
                        if first_screenshot.get("capturedAt")
                        else None
                    ),
                    detail="SOLO 已启动，正在分析首帧截图。",
                    display_index=current_config.solo.preferred_display_index,
                )
                active_solo.log_path = solo_logger.start(envelope.request_id, payload.content)
                await emit_solo_status(active_solo)
                if not active_solo.last_screenshot_path:
                    active_solo.state = "error"
                    active_solo.detail = "首帧截图失败，无法启动 SOLO。"
                    await emit_solo_status(active_solo)
                else:
                    async def _start_agent() -> None:
                        nonlocal solo_service
                        if solo_service is None or not is_solo_running(active_solo):
                            return
                        try:
                            slog(
                                f"request={active_solo.request_id} agent_loop starting "
                                f"task={active_solo.task[:100]}"
                            )
                            solo_logger.write(
                                "agent_start",
                                {"task": active_solo.task},
                            )
                            await emit_solo_trace(
                                active_solo,
                                "agent",
                                "started",
                                "Agent 开始自主决策执行任务...",
                            )
                            await emit_solo_status(active_solo)
                            await agent_loop(active_solo, active_solo.last_screenshot_path)
                        except Exception as exc:  # noqa: BLE001
                            if not is_solo_running(active_solo):
                                return
                            active_solo.state = "error"
                            active_solo.detail = f"Agent 启动失败: {exc}"
                            slog(f"request={active_solo.request_id} agent_start error={exc}")
                            solo_logger.write("error", {"reason": active_solo.detail})
                            await emit_solo_status(active_solo)

                    schedule_solo_task(_start_agent())
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
                    cancel_active_solo_task()
                    solo_logger.write("paused", {"reason": active_solo.detail})
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
                        solo_logger.write("error", {"reason": active_solo.detail})
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
                        schedule_solo_task(
                            agent_loop(
                                active_solo,
                                active_solo.last_screenshot_path,
                            )
                        )
                    continue

                if control.action == "stop":
                    active_solo.state = "aborted"
                    active_solo.detail = "用户已结束 SOLO。"
                    active_solo.completed_at = utc_now()
                    active_solo.pending_confirmation = None
                    cancel_active_solo_task()
                    solo_logger.write("aborted", {"reason": active_solo.detail})
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
                    solo_logger.write(
                        "confirmation_required",
                        {
                            "decision": "allow",
                            "action": pending["action"],
                            "actionArgs": pending["action_args"],
                        },
                    )
                    await emit_solo_status(active_solo)
                    await emit_solo_step(
                        active_solo,
                        step_index=active_solo.step_count + 1,
                        action=pending["action"],
                        action_args=pending["action_args"],
                        thought_summary=pending["thought_summary"],
                        expected_outcome=pending["expected_outcome"],
                        agent_message=str(pending.get("agent_message") or "") or None,
                        screenshot_path=active_solo.last_screenshot_path,
                    )

                    async def _execute_confirmed_and_continue() -> None:
                        result = await execute_solo_step(
                            active_solo,
                            str(pending["action"]),
                            dict(pending["action_args"]),
                        )
                        if not is_solo_running(active_solo):
                            return
                        # Update session state from execution
                        active_solo.step_count += 1
                        action_str = str(pending["action"])
                        if action_str == active_solo.last_action:
                            active_solo.repeat_action_count += 1
                        else:
                            active_solo.repeat_action_count = 1
                        active_solo.last_action = action_str

                        screenshot = result.get("screenshot")
                        if isinstance(screenshot, dict):
                            new_path = screenshot.get("path")
                            new_hash = screenshot.get("contentHash")
                            if isinstance(new_path, str):
                                active_solo.last_screenshot_path = new_path
                            if isinstance(new_hash, str):
                                if new_hash == active_solo.last_screenshot_hash:
                                    active_solo.same_screenshot_count += 1
                                else:
                                    active_solo.same_screenshot_count = 0
                                active_solo.last_screenshot_hash = new_hash

                        result_summary = summarize_solo_step_result(result)
                        if active_solo.history:
                            active_solo.history[-1]["result"] = result_summary

                        if not result.get("success"):
                            active_solo.state = "paused"
                            active_solo.detail = f"确认动作执行失败: {result.get('executionError', 'unknown')}"
                            await emit_solo_status(active_solo)
                            return

                        # Re-enter agent loop
                        next_screenshot = active_solo.last_screenshot_path or ""
                        if next_screenshot:
                            await agent_loop(active_solo, next_screenshot)

                    schedule_solo_task(_execute_confirmed_and_continue())
                    continue

                if control.action == "confirm_reject":
                    active_solo.pending_confirmation = None
                    active_solo.state = "paused"
                    active_solo.detail = "用户拒绝了危险动作，SOLO 已暂停。"
                    solo_logger.write("paused", {"reason": active_solo.detail})
                    await emit_solo_status(active_solo)
                    continue

                if control.action == "step_result":
                    # In agent_loop mode, step results are handled internally.
                    # This handler is kept for backward compatibility but does nothing.
                    slog(f"request={envelope.request_id} step_result ignored (agent_loop mode)")
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
            enhanced_prompt = payload.content
            prev_result = confirmed_tool_results.pop(envelope.conversation_id, None)
            if prev_result:
                enhanced_prompt = f"{prev_result}\n\n用户新消息：{payload.content}"

            await safe_send(
                "server:status",
                envelope.request_id,
                envelope.conversation_id,
                StatusPayload(
                    stage="thinking",
                    detail="后端正在生成回复",
                ).model_dump(),
            )

            agent_service = build_agent_service(
                runtime_state.get_config(),
                confirmation_store=tool_confirmations,
                request_id=envelope.request_id,
                conversation_id=envelope.conversation_id,
            )
            chunks: list[str] = []
            async for event in agent_service.stream_reply(
                envelope.conversation_id,
                enhanced_prompt,
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
                    continue

                if isinstance(event, ReplyToolConfirmation):
                    pending = tool_confirmations.get(event.confirmation_id)
                    if pending:
                        await safe_send(
                            "server:tool_confirmation_required",
                            envelope.request_id,
                            envelope.conversation_id,
                            {"confirmation": pending.to_payload()},
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
