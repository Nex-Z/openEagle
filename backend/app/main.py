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
    SoloPlanItemPayload,
    SoloPlanStatusPayload,
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
from .solo_service import SoloPlan, SoloService, SoloSessionState, summarize_solo_step_result
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

    async def emit_solo_plan(
        session: SoloSessionState,
        current_index: int | None = None,
        current_status: str | None = None,
    ) -> None:
        plan = session.current_plan
        if not plan:
            return
        if current_index is not None and current_status is not None:
            session.step_statuses[current_index] = current_status
        items = []
        for idx, act in enumerate(plan.actions):
            status = session.step_statuses.get(idx, "pending")
            items.append(
                SoloPlanItemPayload(
                    index=idx,
                    action=act.action,
                    description=act.description or act.action,
                    status=status,
                )
            )
        payload = SoloPlanStatusPayload(
            items=items,
            taskAnalysis=plan.task_analysis,
            alternative=plan.alternative,
            agentMessage=plan.agent_message,
            replanCount=session.replan_count,
        ).model_dump(by_alias=True)
        await safe_send(
            "server:solo_plan",
            session.request_id,
            session.conversation_id,
            {"plan": payload},
        )

    async def process_step_result(
        session: SoloSessionState,
        result: dict[str, Any],
    ) -> None:
        if not is_solo_running(session):
            return

        success = bool(result.get("success", False))
        action = str(result.get("action", "unknown"))
        execution_error = result.get("executionError")
        screenshot = result.get("screenshot")
        screenshot_path: str | None = None
        screenshot_at: str | None = None
        screenshot_hash: str | None = None
        if isinstance(screenshot, dict):
            path_value = screenshot.get("path")
            captured_at_value = screenshot.get("capturedAt")
            content_hash_value = screenshot.get("contentHash")
            if isinstance(path_value, str):
                screenshot_path = path_value
            if isinstance(captured_at_value, str):
                screenshot_at = captured_at_value
            if isinstance(content_hash_value, str):
                screenshot_hash = content_hash_value

        result_summary = summarize_solo_step_result(result)
        if session.history:
            last_decision = session.history[-1].get("decision")
            if isinstance(last_decision, dict) and last_decision.get("action") == action:
                session.history[-1]["result"] = result_summary
            else:
                session.history.append(
                    {
                        "step": session.step_count + 1,
                        "decision": {"action": action, "source": "system_recovery"},
                        "result": result_summary,
                        "timestamp": utc_now(),
                    }
                )

        if screenshot_path:
            session.last_screenshot_path = screenshot_path
        if screenshot_hash:
            if screenshot_hash == session.last_screenshot_hash:
                session.same_screenshot_count += 1
            else:
                session.same_screenshot_count = 0
            session.last_screenshot_hash = screenshot_hash
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
            solo_logger.write("error", {"action": action, "result": result})
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
            solo_logger.write("error", {"action": action, "result": result})
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
        solo_logger.write(
            "action_result",
            {
                "step": session.step_count,
                "action": action,
                "screenshotHash": screenshot_hash,
                "result": result,
            },
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
            session.detail = "检测到连续截图无变化，已自动暂停。"
            solo_logger.write("paused", {"reason": session.detail})
            await emit_solo_status(session)
            return

        if not session.last_screenshot_path:
            session.state = "paused"
            session.detail = "缺少新截图，无法继续。"
            solo_logger.write("paused", {"reason": session.detail})
            await emit_solo_status(session)
            return

        if is_solo_running(session):
            if session.current_plan:
                await execute_plan_step(session, session.last_screenshot_path)
            else:
                await decide_and_emit_next_step(session, session.last_screenshot_path)

    async def execute_solo_step(
        session: SoloSessionState,
        action: str,
        action_args: dict[str, Any],
    ) -> None:
        if not is_solo_running(session):
            return

        try:
            execution_result = await asyncio.to_thread(
                solo_tools.execute,
                action,
                action_args,
            )
            if not is_solo_running(session):
                return
            screenshot = execution_result.get("screenshot")
            if not isinstance(screenshot, dict):
                screenshot = await asyncio.to_thread(solo_tools.screenshot)
            if not is_solo_running(session):
                return
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
            if not is_solo_running(session):
                return
            screenshot_payload: dict[str, Any] | None = None
            try:
                screenshot_payload = await asyncio.to_thread(solo_tools.screenshot)
            except Exception:  # noqa: BLE001
                screenshot_payload = None
            if not is_solo_running(session):
                return
            await process_step_result(
                session,
                {
                    "success": False,
                    "action": action,
                    "executionError": str(exc),
                    "screenshot": screenshot_payload,
                },
            )

    async def execute_plan_step(
        session: SoloSessionState,
        screenshot_path: str,
    ) -> None:
        nonlocal solo_service
        if not is_solo_running(session) or solo_service is None:
            return

        plan = session.current_plan
        if not plan or session.plan_step_index >= len(plan.actions):
            session.current_plan = None
            session.replan_count = 0
            session.state = "completed"
            session.completed_at = utc_now()
            session.detail = "计划已执行完毕。"
            slog(f"request={session.request_id} plan completed at step={session.step_count}")
            solo_logger.write("completed", {"step": session.step_count})
            await emit_solo_status(session)
            await safe_send(
                "server:message",
                session.request_id,
                session.conversation_id,
                {"content": "SOLO 任务已完成。"},
            )
            return

        current_action = plan.actions[session.plan_step_index]
        action = current_action.action
        action_args = current_action.action_args

        if action == "finish":
            if session.step_count >= 2:
                session.current_plan = None
                session.replan_count = 0
                session.state = "completed"
                session.completed_at = utc_now()
                session.detail = "SOLO 任务完成。"
                slog(f"request={session.request_id} completed at step={session.step_count}")
                solo_logger.write("completed", {"step": session.step_count})
                await emit_solo_status(session)
                await safe_send(
                    "server:message",
                    session.request_id,
                    session.conversation_id,
                    {"content": "SOLO 任务已完成。"},
                )
                return
            slog(
                f"request={session.request_id} ignored early finish at step={session.step_count}"
            )
            session.plan_step_index += 1
            await execute_plan_step(session, screenshot_path)
            return

        if action == "replan":
            await replan_and_continue(session, screenshot_path, str(action_args.get("reason", "模型请求重新规划")))
            return

        if solo_service.is_batch_executable(action):
            await emit_solo_plan(session, current_index=session.plan_step_index, current_status="in_progress")
            slog(
                f"request={session.request_id} batch_step step={session.step_count + 1} "
                f"action={action} plan_idx={session.plan_step_index}"
            )
            await emit_solo_step(
                session,
                step_index=session.step_count + 1,
                action=action,
                action_args=action_args,
                thought_summary=current_action.description or f"批量执行: {action}",
                expected_outcome=current_action.description,
                screenshot_path=screenshot_path,
            )

            try:
                execution_result = await asyncio.to_thread(
                    solo_executor.execute_batch_action,
                    action,
                    action_args,
                )
                if not is_solo_running(session):
                    return
                session.step_count += 1
                session.plan_step_index += 1
                session.detail = f"批量执行第 {session.step_count} 步: {action}"
                if action == session.last_action:
                    session.repeat_action_count += 1
                else:
                    session.repeat_action_count = 1
                session.last_action = action

                result_summary = {"success": True, "action": action, **execution_result}
                session.history.append(
                    {
                        "step": session.step_count,
                        "decision": {"action": action, "action_args": action_args, "source": "plan_batch"},
                        "result": result_summary,
                        "timestamp": utc_now(),
                    }
                )
                slog(
                    f"request={session.request_id} batch_step_ok step={session.step_count} action={action}"
                )
                solo_logger.write(
                    "batch_step",
                    {"step": session.step_count, "action": action, "result": result_summary},
                )
                await emit_solo_trace(
                    session,
                    "step_result",
                    "completed",
                    f"批量执行: {action}",
                    params={"action": action, "step": session.step_count},
                    result=result_summary,
                )
                await emit_solo_status(session)
                await emit_solo_plan(session, current_index=session.plan_step_index - 1, current_status="completed")

                if session.step_count >= session.max_steps:
                    session.state = "paused"
                    session.detail = f"超过最大步数 {session.max_steps}，已自动暂停。"
                    solo_logger.write("paused", {"reason": session.detail})
                    await emit_solo_status(session)
                    return

                if action == "execute_command" and not execution_result.get("ok"):
                    fresh_screenshot = await asyncio.to_thread(solo_tools.screenshot)
                    fresh_path = fresh_screenshot.get("path") if isinstance(fresh_screenshot, dict) else None
                    await replan_and_continue(
                        session,
                        str(fresh_path) if fresh_path else screenshot_path,
                        f"命令执行失败: {str(execution_result.get('output', ''))[:200]}",
                    )
                    return

                await execute_plan_step(session, screenshot_path)

            except Exception as exc:  # noqa: BLE001
                if not is_solo_running(session):
                    return
                session.state = "paused"
                session.detail = f"批量执行失败: {exc}"
                slog(f"request={session.request_id} batch_step error={exc}")
                solo_logger.write("error", {"action": action, "error": str(exc)})
                await emit_solo_trace(
                    session,
                    "step_result",
                    "error",
                    f"批量执行失败: {action}",
                    params={"action": action},
                    result=str(exc),
                )
                await emit_solo_status(session)
                await emit_solo_plan(session, current_index=session.plan_step_index, current_status="failed")
            return

        try:
            await emit_solo_plan(session, current_index=session.plan_step_index, current_status="in_progress")
            app_context = _infer_app_context(session.task)
            decision = await solo_service.decide_next(
                task=session.task,
                screenshot_path=screenshot_path,
                history=session.history,
                display_index=session.display_index,
                app_context=app_context,
            )
            if not is_solo_running(session):
                return

            if decision.action == "replan":
                await replan_and_continue(session, screenshot_path, decision.agent_message or "模型请求重新规划")
                return

            final_action = decision.action
            final_args = decision.action_args

            assessment = assess_solo_action(final_action, final_args, workspace_root)
            if assessment.level == "blocked":
                session.state = "paused"
                session.detail = f"动作已阻断: {assessment.reason}"
                solo_logger.write(
                    "paused",
                    {"reason": session.detail, "action": final_action, "actionArgs": final_args},
                )
                await emit_solo_status(session)
                return

            permission_mode = runtime_state.get_config().permissions.mode
            if assessment.level == "confirm" and permission_mode != "all":
                session.state = "waiting_user_confirmation"
                session.pending_confirmation = {
                    "action": final_action,
                    "action_args": final_args,
                    "thought_summary": decision.thought_summary,
                    "expected_outcome": decision.expected_outcome,
                    "agent_message": decision.agent_message,
                    "risk_level": assessment.level,
                    "reason": assessment.reason,
                }
                session.detail = "检测到危险动作，等待用户确认。"
                slog(
                    f"request={session.request_id} waiting confirmation action={final_action} reason={assessment.reason}"
                )
                solo_logger.write(
                    "confirmation_required",
                    {
                        "step": session.step_count + 1,
                        "action": final_action,
                        "actionArgs": final_args,
                        "reason": assessment.reason,
                    },
                )
                await emit_solo_status(session)
                await emit_confirmation(
                    session,
                    step_index=session.step_count + 1,
                    reason=assessment.reason,
                    action=final_action,
                    action_args=final_args,
                    thought_summary=decision.thought_summary,
                )
                return

            slog(
                f"request={session.request_id} visual_decision action={final_action} "
                f"done={decision.is_task_done} step={session.step_count + 1}"
            )
            await emit_solo_trace(
                session,
                "decision",
                "completed",
                f"视觉决策: {final_action}",
                params={
                    "thought": decision.thought_summary,
                    "expected_outcome": decision.expected_outcome,
                },
                result=solo_service.decision_dict(decision),
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
                action=final_action,
                action_args=final_args,
                thought_summary=decision.thought_summary,
                expected_outcome=decision.expected_outcome,
                agent_message=decision.agent_message,
                screenshot_path=screenshot_path,
            )

            execution_result = await asyncio.to_thread(
                solo_tools.execute,
                final_action,
                final_args,
            )
            if not is_solo_running(session):
                return

            new_screenshot = execution_result.get("screenshot")
            if not isinstance(new_screenshot, dict):
                new_screenshot = await asyncio.to_thread(solo_tools.screenshot)
            if not is_solo_running(session):
                return

            session.step_count += 1
            session.plan_step_index += 1
            if final_action == session.last_action:
                session.repeat_action_count += 1
            else:
                session.repeat_action_count = 1
            session.last_action = final_action

            if isinstance(new_screenshot, dict):
                new_path = new_screenshot.get("path")
                new_hash = new_screenshot.get("contentHash")
                if isinstance(new_path, str):
                    session.last_screenshot_path = new_path
                if isinstance(new_hash, str):
                    if new_hash == session.last_screenshot_hash:
                        session.same_screenshot_count += 1
                    else:
                        session.same_screenshot_count = 0
                    session.last_screenshot_hash = new_hash

            if session.same_screenshot_count >= 3:
                await replan_and_continue(
                    session,
                    session.last_screenshot_path or screenshot_path,
                    "连续截图无变化，需要重新规划",
                )
                return

            if session.step_count >= session.max_steps:
                session.state = "paused"
                session.detail = f"超过最大步数 {session.max_steps}，已自动暂停。"
                solo_logger.write("paused", {"reason": session.detail})
                await emit_solo_status(session)
                return

            result_summary = summarize_solo_step_result(
                {"success": True, "action": final_action, "executionResult": execution_result, "screenshot": new_screenshot}
            )
            session.history[-1]["result"] = result_summary
            solo_logger.write(
                "action_result",
                {
                    "step": session.step_count,
                    "action": final_action,
                    "result": result_summary,
                },
            )
            await emit_solo_trace(
                session,
                "step_result",
                "completed",
                f"视觉动作结果: {final_action}",
                params={"action": final_action, "step": session.step_count},
                result=result_summary,
            )
            await emit_solo_status(session)
            await emit_solo_plan(session, current_index=session.plan_step_index - 1, current_status="completed")

            next_screenshot = session.last_screenshot_path or screenshot_path
            await execute_plan_step(session, next_screenshot)

        except Exception as exc:  # noqa: BLE001
            if not is_solo_running(session):
                return
            session.state = "paused"
            session.detail = f"视觉决策失败: {exc}"
            slog(f"request={session.request_id} visual_decision error={exc}")
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_trace(
                session,
                "decision",
                "error",
                "视觉决策失败",
                result=str(exc),
            )
            await emit_solo_status(session)
            await emit_solo_plan(session, current_index=session.plan_step_index, current_status="failed")

    async def replan_and_continue(
        session: SoloSessionState,
        screenshot_path: str,
        failure_reason: str,
    ) -> None:
        nonlocal solo_service
        if not is_solo_running(session) or solo_service is None:
            return

        session.replan_count += 1
        if session.replan_count > 3:
            session.state = "paused"
            session.detail = f"已重新规划 {session.replan_count} 次，任务可能无法完成。"
            solo_logger.write("paused", {"reason": session.detail})
            await emit_solo_status(session)
            return

        if session.current_plan:
            for idx in range(session.plan_step_index, len(session.current_plan.actions)):
                if session.step_statuses.get(idx) not in ("completed", "failed"):
                    session.step_statuses[idx] = "skipped"
            await emit_solo_plan(session)

        slog(f"request={session.request_id} replan count={session.replan_count} reason={failure_reason}")
        await emit_solo_trace(
            session,
            "replan",
            "started",
            f"第 {session.replan_count} 次重新规划: {failure_reason}",
            params={"reason": failure_reason},
        )

        try:
            app_context = _infer_app_context(session.task)
            completed = [
                h.get("decision", {})
                for h in session.history
                if isinstance(h.get("decision"), dict)
            ]
            remaining = (
                session.current_plan.actions[session.plan_step_index:]
                if session.current_plan
                else []
            )
            remaining_dicts = [
                {"action": a.action, "action_args": a.action_args, "description": a.description}
                for a in remaining
            ]

            new_plan = await solo_service.decide_plan(
                task=session.task,
                screenshot_path=screenshot_path,
                display_index=session.display_index,
                app_context=app_context,
            )
            if not is_solo_running(session):
                return

            session.current_plan = new_plan
            session.plan_step_index = 0
            session.detail = f"已重新规划，共 {len(new_plan.actions)} 步。"
            solo_logger.write(
                "replan",
                {
                    "count": session.replan_count,
                    "reason": failure_reason,
                    "planSteps": len(new_plan.actions),
                    "analysis": new_plan.task_analysis,
                },
            )
            await emit_solo_trace(
                session,
                "replan",
                "completed",
                f"重新规划完成，共 {len(new_plan.actions)} 步",
                params={
                    "analysis": new_plan.task_analysis,
                    "alternative": new_plan.alternative,
                    "agentMessage": new_plan.agent_message,
                },
            )
            await emit_solo_status(session)
            await emit_solo_plan(session)
            await execute_plan_step(session, screenshot_path)

        except Exception as exc:  # noqa: BLE001
            if not is_solo_running(session):
                return
            session.state = "error"
            session.detail = f"重新规划失败: {exc}"
            slog(f"request={session.request_id} replan error={exc}")
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_status(session)

    async def decide_and_emit_next_step(session: SoloSessionState, screenshot_path: str) -> None:
        nonlocal solo_service
        if not is_solo_running(session):
            return

        if solo_service is None:
            session.state = "error"
            session.detail = "SOLO 服务未初始化。"
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_status(session)
            return

        try:
            app_context = _infer_app_context(session.task)
            decision = await solo_service.decide_next(
                task=session.task,
                screenshot_path=screenshot_path,
                history=session.history,
                display_index=session.display_index,
                app_context=app_context,
            )
            if not is_solo_running(session):
                return
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
        except Exception as exc:  # noqa: BLE001
            session.state = "error"
            session.detail = f"VL 推理失败: {exc}"
            slog(f"request={session.request_id} decision error={exc}")
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_trace(
                session,
                "decision",
                "error",
                "VL 推理失败",
                result=str(exc),
            )
            await emit_solo_status(session)
            return

        if not is_solo_running(session):
            return

        if decision.action == "finish" and session.step_count >= 2:
            session.history.append(
                {
                    "step": session.step_count + 1,
                    "decision": solo_service.decision_dict(decision),
                    "timestamp": utc_now(),
                }
            )
            session.state = "completed"
            session.completed_at = utc_now()
            session.detail = "SOLO 任务完成。"
            slog(f"request={session.request_id} completed at step={session.step_count}")
            solo_logger.write("completed", {"step": session.step_count})
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
            wait_args = {"ms": 600}
            await emit_solo_step(
                session,
                step_index=session.step_count + 1,
                action="wait",
                action_args=wait_args,
                thought_summary="模型过早结束，系统要求至少完成两步执行后再结束。",
                expected_outcome="等待后继续基于新截图决策",
                screenshot_path=screenshot_path,
            )
            await execute_solo_step(session, "wait", wait_args)
            return

        session.history.append(
            {
                "step": session.step_count + 1,
                "decision": solo_service.decision_dict(decision),
                "timestamp": utc_now(),
            }
        )

        assessment = assess_solo_action(decision.action, decision.action_args, workspace_root)
        if assessment.level == "blocked":
            session.state = "paused"
            session.detail = f"动作已阻断: {assessment.reason}"
            solo_logger.write(
                "paused",
                {
                    "reason": session.detail,
                    "action": decision.action,
                    "actionArgs": decision.action_args,
                },
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
                "expected_outcome": decision.expected_outcome,
                "agent_message": decision.agent_message,
                "risk_level": assessment.level,
                "reason": assessment.reason,
            }
            session.detail = "检测到危险动作，等待用户确认。"
            slog(
                f"request={session.request_id} waiting confirmation action={decision.action} reason={assessment.reason}"
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
            return

        await emit_solo_step(
            session,
            step_index=session.step_count + 1,
            action=decision.action,
            action_args=decision.action_args,
            thought_summary=decision.thought_summary,
            expected_outcome=decision.expected_outcome,
            agent_message=decision.agent_message,
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
                    async def _plan_and_start() -> None:
                        nonlocal solo_service
                        if solo_service is None or not is_solo_running(active_solo):
                            return
                        try:
                            await emit_solo_trace(
                                active_solo,
                                "planning",
                                "started",
                                "正在分析任务并制定执行计划...",
                            )
                            await emit_solo_status(active_solo)
                            app_context = _infer_app_context(active_solo.task)
                            plan = await solo_service.decide_plan(
                                task=active_solo.task,
                                screenshot_path=active_solo.last_screenshot_path,
                                display_index=active_solo.display_index,
                                app_context=app_context,
                            )
                            if not is_solo_running(active_solo):
                                return
                            active_solo.current_plan = plan
                            active_solo.plan_step_index = 0
                            active_solo.detail = f"规划完成，共 {len(plan.actions)} 步。"
                            slog(
                                f"request={active_solo.request_id} plan_ready "
                                f"steps={len(plan.actions)} analysis={plan.task_analysis[:100]}"
                            )
                            solo_logger.write(
                                "plan",
                                {
                                    "taskAnalysis": plan.task_analysis,
                                    "alternative": plan.alternative,
                                    "estimatedSteps": plan.estimated_steps,
                                    "actualSteps": len(plan.actions),
                                    "agentMessage": plan.agent_message,
                                },
                            )
                            await emit_solo_trace(
                                active_solo,
                                "planning",
                                "completed",
                                f"执行计划已制定: {len(plan.actions)} 步",
                                params={
                                    "taskAnalysis": plan.task_analysis,
                                    "alternative": plan.alternative,
                                    "agentMessage": plan.agent_message,
                                },
                            )
                            await emit_solo_status(active_solo)
                            await emit_solo_plan(active_solo)
                            await execute_plan_step(active_solo, active_solo.last_screenshot_path)
                        except Exception as exc:  # noqa: BLE001
                            if not is_solo_running(active_solo):
                                return
                            active_solo.state = "error"
                            active_solo.detail = f"规划失败: {exc}"
                            slog(f"request={active_solo.request_id} planning error={exc}")
                            solo_logger.write("error", {"reason": active_solo.detail})
                            await emit_solo_status(active_solo)

                    schedule_solo_task(_plan_and_start())
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
                            execute_plan_step(
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
                    schedule_solo_task(
                        execute_solo_step(
                            active_solo,
                            str(pending["action"]),
                            dict(pending["action_args"]),
                        )
                    )
                    continue

                if control.action == "confirm_reject":
                    active_solo.pending_confirmation = None
                    active_solo.state = "paused"
                    active_solo.detail = "用户拒绝了危险动作，SOLO 已暂停。"
                    solo_logger.write("paused", {"reason": active_solo.detail})
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
