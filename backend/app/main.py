from __future__ import annotations

import argparse
import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent_runtime import AgentRuntime
from .attachments import AttachmentError, AttachmentStore, append_attachment_context
from .config import AppConfig, load_config
from .confirmations import ToolConfirmationStore
from .default_tools import build_default_tools, execute_confirmed_tool
from .im.bridge import IMBridge, bind_config_getter
from .im.models import IMConversationBinding
from .models import (
    AttachmentRef,
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
from .runtime_state import RuntimeState
from .safety import assess_solo_action, is_repairable_solo_block
from .solo_capabilities import SoloCapabilityRuntime
from .solo_executor import SoloExecutor
from .solo_kernel import SoloAgentKernel, action_signature
from .solo_run_logger import SoloRunLogger
from .solo_service import SoloService, SoloSessionState, summarize_solo_step_result
from .solo_toolkit import SoloToolkit

app = FastAPI(title="openEagle Agent Backend")
config = load_config()
runtime_state = RuntimeState()
runtime_state.update_config(config)
workspace_root = Path(__file__).resolve().parents[2]
attachment_store = AttachmentStore(workspace_root)
confirmed_tool_results: dict[str, str] = {}
ATTACHMENT_WS_MAX_SIZE = 192 * 1024 * 1024

POST_ACTION_CAPTURE_DELAYS_MS: dict[str, list[int]] = {
    "click": [180, 420, 800],
    "double_click": [240, 520, 900],
    "right_click": [180, 420, 800],
    "move_mouse": [80, 180],
    "scroll": [180, 420, 800],
    "type_text": [160, 360, 700],
    "press_keys": [260, 620, 1000],
    "execute_command": [420, 900, 1400],
    "open_url": [700, 1400, 2200],
    "wait": [0],
}
POST_ACTION_STABLE_CAPTURE_ACTIONS = {"click", "double_click", "press_keys", "open_url"}


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


def record_solo_action_progress(
    session: SoloSessionState,
    action: str,
    action_args: dict[str, Any],
) -> str:
    signature = action_signature(action, action_args)
    if action == session.last_action:
        session.repeat_action_count += 1
    else:
        session.repeat_action_count = 1
    if signature == session.last_action_signature:
        session.repeat_action_signature_count += 1
    else:
        session.repeat_action_signature_count = 1
    session.last_action = action
    session.last_action_signature = signature
    return signature


def solo_stability_summary(session: SoloSessionState, final_reason: str) -> dict[str, Any]:
    return {
        "finalReason": final_reason,
        "totalSteps": session.step_count,
        "noOpCount": session.no_op_count,
        "uncertainCount": session.uncertain_count,
        "failedCount": session.failed_count,
        "recoveryModeEntries": session.recovery_mode_entries,
        "batchSuppressedCount": session.batch_suppressed_count,
        "repeatActionSignatureCount": session.repeat_action_signature_count,
        "sameScreenshotCount": session.same_screenshot_count,
        "lastAction": session.last_action,
        "lastActionSignature": session.last_action_signature,
    }


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
    solo_kernel: SoloAgentKernel | None = None
    solo_capabilities: SoloCapabilityRuntime | None = None
    solo_default_tools = build_default_tools(
        workspace_root=workspace_root,
        builtin_tools=[bt.model_dump() for bt in config.builtin_tools],
    )
    solo_executor = SoloExecutor(default_tools=solo_default_tools)
    solo_tools = SoloToolkit(solo_executor)
    solo_logger = SoloRunLogger(workspace_root)
    tool_confirmations = ToolConfirmationStore()
    im_bridge: IMBridge | None = None

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
            await close_solo_capabilities()

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

    async def close_solo_capabilities() -> None:
        nonlocal solo_capabilities
        if solo_capabilities is None:
            return
        try:
            await solo_capabilities.close()
        except Exception as exc:  # noqa: BLE001
            slog(f"capability close error={exc}")
        finally:
            solo_capabilities = None

    def assess_current_solo_action(action: str, action_args: dict[str, Any]):
        if solo_capabilities is not None:
            capability_assessment = solo_capabilities.assess_action(action, action_args)
            if capability_assessment is not None:
                return capability_assessment
        return assess_solo_action(action, action_args, workspace_root)

    async def emit_solo_trace(
        session: SoloSessionState,
        name: str,
        status: str,
        summary: str,
        params: dict[str, Any] | None = None,
        result: Any | None = None,
        kind: str = "skill",
    ) -> None:
        now = utc_now()
        trace_status = status if status in {"started", "completed", "error"} else "completed"
        trace = {
            "id": f"solo-trace-{session.step_count}-{name}-{status}",
            "kind": kind,
            "name": f"SOLO/{name}",
            "status": trace_status,
            "summary": summary,
            "params": params or {},
            "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
            "startedAt": now,
        }
        if trace_status != "started":
            trace["completedAt"] = now
        await safe_send(
            "server:trace",
            session.request_id,
            session.conversation_id,
            {"trace": trace},
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
        if (
            im_bridge is not None
            and session.conversation_id.startswith("im_")
            and session.detail
            and session.state in {"paused", "waiting_user_confirmation", "completed", "aborted", "error"}
        ):
            await im_bridge.send_text(
                session.conversation_id,
                f"SOLO {session.state}: {session.detail}",
            )

    async def emit_solo_plan(session: SoloSessionState, kernel: SoloAgentKernel) -> None:
        await safe_send(
            "server:solo_plan",
            session.request_id,
            session.conversation_id,
            {"plan": kernel.plan_payload()},
        )

    async def emit_solo_screenshot(
        session: SoloSessionState,
        screenshot: dict[str, Any],
        label: str,
    ) -> None:
        await safe_send(
            "server:solo_screenshot",
            session.request_id,
            session.conversation_id,
            {"screenshot": screenshot, "label": label},
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
        findings: list[str] | None = None,
        confidence: float | None = None,
        screen_state: str | None = None,
    ) -> None:
        payload = SoloStepPayload(
            stepIndex=step_index,
            action=action,
            actionArgs=action_args,
            thoughtSummary=thought_summary,
            agentMessage=agent_message,
            expectedOutcome=expected_outcome,
            findings=findings or [],
            confidence=confidence,
            screenState=screen_state,
            screenshotPath=screenshot_path,
            timestamp=utc_now(),
        ).model_dump(by_alias=True)
        await safe_send(
            "server:solo_step",
            session.request_id,
            session.conversation_id,
            {"step": payload},
        )
        if im_bridge is not None and session.conversation_id.startswith("im_"):
            visible_text = agent_message or expected_outcome or thought_summary
            if action == "finish":
                await im_bridge.send_text(session.conversation_id, visible_text)
            elif visible_text:
                await im_bridge.send_text(
                    session.conversation_id,
                    f"SOLO 第 {step_index} 步: {action}\n{visible_text}",
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
        if im_bridge is not None and session.conversation_id.startswith("im_"):
            await im_bridge.send_text(
                session.conversation_id,
                f"SOLO 需要确认动作 `{action}`。\n原因: {reason}\n回复 /allow 继续，或 /reject 拒绝。",
            )


    def _build_final_report(session: SoloSessionState, decision: "SoloDecision | None" = None) -> str:
        # finish_report is the full final report; agent_message is often just a teaser.
        # Always prefer the richer content.
        if decision:
            finish = getattr(decision, "finish_report", "") or ""
            agent = decision.agent_message or ""
            best = finish if len(finish) >= len(agent) else agent
            if best:
                return best
        if session.last_agent_message:
            return session.last_agent_message
        return "抱歉，任务执行过程中出现了问题，未能完成。"

    async def execute_solo_step(
        session: SoloSessionState,
        action: str,
        action_args: dict[str, Any],
        capture_after: bool = True,
    ) -> dict[str, Any]:
        """Execute a single SOLO action and return the execution result dict.

        Returns a dict with keys: success, action, executionResult, screenshot.
        On exception returns success=False with executionError.
        Does NOT update session.step_count or loop — caller is responsible.
        """
        if not is_solo_running(session):
            return {"success": False, "action": action, "executionError": "session not running"}

        try:
            if action in {"run_configured_tool", "call_mcp_tool"} and solo_capabilities is not None:
                execution_result = await solo_capabilities.execute_action_async(action, action_args)
            else:
                execution_result = await asyncio.to_thread(
                    solo_tools.execute,
                    action,
                    action_args,
                )
            if not is_solo_running(session):
                return {"success": False, "action": action, "executionError": "session stopped"}
            screenshot = execution_result.get("screenshot")
            capture_attempts = 1 if isinstance(screenshot, dict) else 0
            post_action_delay_ms = 0
            post_action_total_delay_ms = 0
            visual_change = None
            used_virtual_capture = False
            stable_after_change = None
            stability_samples = 0
            if not capture_after:
                return {
                    "success": True,
                    "action": action,
                    "executionResult": execution_result,
                    "screenshot": screenshot,
                    "captureAttempts": capture_attempts,
                    "postActionDelayMs": post_action_delay_ms,
                    "postActionTotalDelayMs": post_action_total_delay_ms,
                    "visualChange": visual_change,
                    "usedVirtualCapture": used_virtual_capture,
                    "stableAfterChange": stable_after_change,
                    "stabilitySamples": stability_samples,
                }
            if not isinstance(screenshot, dict):
                delays = POST_ACTION_CAPTURE_DELAYS_MS.get(action, [220, 520, 900])
                wait_for_stable_frame = action in POST_ACTION_STABLE_CAPTURE_ACTIONS
                saw_visual_change = False
                previous_capture_hash: str | None = None
                for delay_ms in delays:
                    post_action_delay_ms = delay_ms
                    post_action_total_delay_ms += delay_ms
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000)
                    screenshot = await asyncio.to_thread(solo_tools.screenshot)
                    capture_attempts += 1
                    if not is_solo_running(session):
                        return {"success": False, "action": action, "executionError": "session stopped"}
                    if not isinstance(screenshot, dict):
                        continue
                    new_hash = screenshot.get("contentHash")
                    if not isinstance(new_hash, str):
                        continue
                    changed_from_last = (
                        not session.last_screenshot_hash or new_hash != session.last_screenshot_hash
                    )
                    if changed_from_last:
                        visual_change = True
                        if wait_for_stable_frame:
                            if saw_visual_change and new_hash == previous_capture_hash:
                                stability_samples += 1
                                stable_after_change = True
                                break
                            saw_visual_change = True
                            previous_capture_hash = new_hash
                            stable_after_change = False
                            continue
                        break
                    visual_change = False
                    if wait_for_stable_frame and saw_visual_change:
                        if new_hash == previous_capture_hash:
                            stability_samples += 1
                            stable_after_change = True
                            break
                        previous_capture_hash = new_hash
                        stable_after_change = False
                if visual_change is False:
                    solo_executor.set_capture_all_displays(True)
                    used_virtual_capture = True
                    await asyncio.sleep(0.2)
                    screenshot = await asyncio.to_thread(solo_tools.screenshot)
                    capture_attempts += 1
                    post_action_total_delay_ms += 200
                    if isinstance(screenshot, dict):
                        new_hash = screenshot.get("contentHash")
                        if isinstance(new_hash, str) and (
                            not session.last_screenshot_hash or new_hash != session.last_screenshot_hash
                        ):
                            visual_change = True
            return {
                "success": True,
                "action": action,
                "executionResult": execution_result,
                "screenshot": screenshot,
                "captureAttempts": capture_attempts,
                "postActionDelayMs": post_action_delay_ms,
                "postActionTotalDelayMs": post_action_total_delay_ms,
                "visualChange": visual_change,
                "usedVirtualCapture": used_virtual_capture,
                "stableAfterChange": stable_after_change,
                "stabilitySamples": stability_samples,
            }
        except Exception as exc:  # noqa: BLE001
            if not is_solo_running(session):
                return {"success": False, "action": action, "executionError": "session stopped"}
            screenshot_payload: dict[str, Any] | None = None
            try:
                await asyncio.sleep(0.25)
                screenshot_payload = await asyncio.to_thread(solo_tools.screenshot)
            except Exception:  # noqa: BLE001
                screenshot_payload = None
            return {
                "success": False,
                "action": action,
                "executionError": str(exc),
                "screenshot": screenshot_payload,
                "captureAttempts": 1 if screenshot_payload else 0,
                "postActionDelayMs": 250,
                "postActionTotalDelayMs": 250,
                "visualChange": False,
            }

    async def agent_loop(
        session: SoloSessionState,
        screenshot_path: str,
    ) -> None:
        """Core observe→think→act loop. Replaces execute_plan_step + replan_and_continue + decide_and_emit_next_step."""
        nonlocal solo_service, solo_kernel
        if solo_service is None:
            session.state = "error"
            session.detail = "SOLO 服务未初始化。"
            solo_logger.write("error", {"reason": session.detail})
            await emit_solo_status(session)
            await close_solo_capabilities()
            return
        if solo_kernel is None:
            solo_kernel = SoloAgentKernel.create(session.task)
            await emit_solo_plan(session, solo_kernel)

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
                    kernel_state=solo_kernel.prompt_context(),
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
                await close_solo_capabilities()
                return

            if not is_solo_running(session):
                return

            if decision.agent_message:
                session.last_agent_message = decision.agent_message

            for trace in decision.tool_traces:
                await emit_solo_trace(
                    session,
                    str(trace.get("name") or "capability"),
                    str(trace.get("status") or "completed"),
                    str(trace.get("summary") or "SOLO 能力调用"),
                    params=trace.get("params") if isinstance(trace.get("params"), dict) else {},
                    result=trace.get("result"),
                    kind=str(trace.get("kind") or "tool"),
                )

            # Sanitize thought_summary: if VL returned a JSON object as the string,
            # extract the actual text so the frontend doesn't show raw JSON.
            if decision.thought_summary:
                ts = decision.thought_summary.strip()
                if ts.startswith("{"):
                    try:
                        parsed = json.loads(ts)
                        if isinstance(parsed, dict):
                            decision = decision.model_copy(
                                update={
                                    "thought_summary": (
                                        parsed.get("thought_summary")
                                        or parsed.get("analysis")
                                        or parsed.get("progress")
                                        or str(parsed)
                                    )
                                }
                            )
                    except (json.JSONDecodeError, ValueError):
                        pass

            # Accumulate findings
            if decision.findings:
                for finding in decision.findings:
                    if finding not in session.findings:
                        session.findings.append(finding)

            if decision.is_task_done and decision.action != "finish":
                slog(
                    f"request={session.request_id} ignoring passive is_task_done "
                    f"for non-finish action={decision.action}"
                )
                decision = decision.model_copy(update={"is_task_done": False})

            # ━━ STEP 2: Check completion ━━
            # Guard: reject finish/is_task_done if the task has no completion evidence.
            # The kernel decides what evidence means for this task: answer content for
            # information tasks, artifact/verifier details for file/code tasks, and a
            # visible target state for app/GUI tasks.
            #
            # This keeps SOLO from ending with "page opened" or an empty "done" report.
            premature_finish = False
            finish_requested = decision.action == "finish"
            if finish_requested and not solo_kernel.has_completion_evidence(session.findings, decision):
                premature_finish = True
                reason = solo_kernel.completion_block_reason()
                slog(
                    f"request={session.request_id} rejecting finish "
                    f"without completion evidence step={session.step_count} last_action={session.last_action}"
                )
                if solo_kernel.reject_premature_finish(reason):
                    await emit_solo_plan(session, solo_kernel)
                await emit_solo_trace(
                    session,
                    "decision",
                    "error",
                    "拒绝空结果完成",
                    params={
                        "action": decision.action,
                        "is_task_done": decision.is_task_done,
                        "completion_mode": solo_kernel.completion_mode(),
                        "completion_requirement": solo_kernel.completion_requirement(),
                    },
                    result=reason,
                )
                decision = solo_service._fallback_decision_from_text(
                    "premature finish rejected",
                    ValueError(reason),
                )
                decision.agent_message = "完成证据还不够，我会继续观察、执行并验证结果。"
                decision.progress = reason

            if solo_kernel.record_decision(decision):
                await emit_solo_plan(session, solo_kernel)

            slog(
                f"request={session.request_id} decision action={decision.action} "
                f"done={decision.is_task_done} step={session.step_count + 1}"
            )

            if (decision.is_task_done or decision.action == "finish") and not premature_finish:
                await emit_solo_trace(
                    session,
                    "decision",
                    "completed",
                    f"视觉决策: {decision.action}",
                    params={
                        "thought": decision.thought_summary,
                        "expected_outcome": decision.progress or decision.expected_outcome,
                        "screen_state": decision.screen_state,
                        "confidence": decision.confidence,
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
                        "completion": True,
                    },
                )
                report = _build_final_report(session, decision)
                # Emit final step so the report appears as a regular chat message
                # (same display as intermediate steps via appendSoloMessage).
                await emit_solo_step(
                    session,
                    step_index=session.step_count + 1,
                    action="finish",
                    action_args={},
                    thought_summary=decision.thought_summary,
                    expected_outcome=decision.progress or decision.expected_outcome or report,
                    agent_message=report,
                    findings=decision.findings or session.findings or None,
                    confidence=decision.confidence,
                    screen_state=decision.screen_state,
                )
                session.state = "completed"
                session.completed_at = utc_now()
                session.detail = None
                slog(f"request={session.request_id} completed at step={session.step_count}")
                solo_logger.write("completed", {"step": session.step_count, "source": "agent_loop"})
                solo_logger.write("stability_summary", solo_stability_summary(session, "completed"))
                await emit_solo_plan(session, solo_kernel)
                await emit_solo_status(session)
                await close_solo_capabilities()
                return

            planned_actions = [
                {"action": decision.action, "action_args": decision.action_args, "batch_index": 0}
            ]
            if solo_kernel.recovery_mode and decision.batch_actions:
                session.batch_suppressed_count += len(decision.batch_actions)
                solo_logger.write(
                    "batch_suppressed",
                    {
                        "step": session.step_count + 1,
                        "reason": "recovery_mode",
                        "count": len(decision.batch_actions),
                    },
                )
            else:
                for batch_index, item in enumerate(decision.batch_actions, start=1):
                    planned_actions.append(
                        {
                            "action": str(item["action"]),
                            "action_args": dict(item.get("action_args") or {}),
                            "batch_index": batch_index,
                        }
                    )

            # ━━ STEP 3: Safety checks ━━
            permission_mode = runtime_state.get_config().permissions.mode
            blocking_action: dict[str, Any] | None = None
            assessment = None
            if permission_mode != "all":
                for planned_action in planned_actions:
                    current_assessment = assess_current_solo_action(
                        str(planned_action["action"]),
                        dict(planned_action["action_args"]),
                    )
                    if current_assessment.level == "blocked":
                        blocking_action = planned_action
                        assessment = current_assessment
                        break
                    if current_assessment.level == "confirm" and assessment is None:
                        blocking_action = planned_action
                        assessment = current_assessment

                if assessment is None:
                    assessment = assess_current_solo_action(decision.action, decision.action_args)

            if assessment is not None and assessment.level == "blocked":
                session.state = "paused"
                session.detail = f"动作已阻断: {assessment.reason}"
                blocked_action = blocking_action or planned_actions[0]
                blocked_action_name = str(blocked_action["action"])
                blocked_action_args = dict(blocked_action["action_args"])
                if is_repairable_solo_block(blocked_action_name, assessment.reason):
                    session.state = "running"
                    outcome = solo_kernel.record_repairable_action_block(assessment.reason)
                    session.detail = f"SOLO 正在恢复: {outcome.recovery_hint}"
                    session.failed_count += 1
                    session.recovery_mode_entries += 1
                    blocked_decision = decision.model_copy(
                        update={
                            "action": blocked_action_name,
                            "action_args": blocked_action_args,
                            "batch_actions": [],
                            "progress": assessment.reason,
                        }
                    )
                    result_summary = {
                        "success": False,
                        "action": blocked_action_name,
                        "executionError": assessment.reason,
                        "outcomeClass": outcome.outcome_class,
                        "blockedBeforeExecution": True,
                        "repairable": True,
                    }
                    session.history.append(
                        {
                            "step": session.step_count + 1,
                            "decision": solo_service.decision_dict(blocked_decision),
                            "timestamp": utc_now(),
                            "result": result_summary,
                        }
                    )
                    solo_logger.write(
                        "action_feedback",
                        {
                            "step": session.step_count + 1,
                            "action": blocked_action_name,
                            "actionArgs": blocked_action_args,
                            "reason": assessment.reason,
                            "outcomeClass": outcome.outcome_class,
                        },
                    )
                    await emit_solo_trace(
                        session,
                        "decision",
                        "error",
                        "动作参数无效，已反馈给 SOLO 重新决策",
                        params={
                            "action": blocked_action_name,
                            "reason": assessment.reason,
                            "repairable": True,
                        },
                        result=assessment.reason,
                    )
                    if outcome.plan_changed:
                        await emit_solo_plan(session, solo_kernel)
                    await emit_solo_status(session)
                    if outcome.should_pause:
                        session.state = "paused"
                        session.detail = f"SOLO 连续修复动作参数失败，已暂停: {outcome.pause_reason}"
                        solo_logger.write(
                            "paused",
                            {
                                "reason": session.detail,
                                "action": blocked_action_name,
                                "actionArgs": blocked_action_args,
                            },
                        )
                        solo_logger.write(
                            "stability_summary",
                            solo_stability_summary(session, "repairable_action_block_failed"),
                        )
                        await emit_solo_status(session)
                        return
                    continue
                solo_logger.write(
                    "paused",
                    {
                        "reason": session.detail,
                        "action": blocked_action_name,
                        "actionArgs": blocked_action_args,
                    },
                )
                await emit_solo_status(session)
                return

            if assessment is not None and assessment.level == "confirm":
                confirm_action = blocking_action or planned_actions[0]
                session.state = "waiting_user_confirmation"
                session.pending_confirmation = {
                    "action": confirm_action["action"],
                    "action_args": confirm_action["action_args"],
                    "thought_summary": decision.thought_summary,
                    "expected_outcome": decision.progress or decision.expected_outcome,
                    "agent_message": decision.agent_message,
                    "risk_level": assessment.level,
                    "reason": assessment.reason,
                }
                session.detail = "检测到危险动作，等待用户确认。"
                slog(
                    f"request={session.request_id} waiting confirmation "
                    f"action={confirm_action['action']} reason={assessment.reason}"
                )
                solo_logger.write(
                    "confirmation_required",
                    {
                        "step": session.step_count + 1,
                        "action": confirm_action["action"],
                        "actionArgs": confirm_action["action_args"],
                        "reason": assessment.reason,
                    },
                )
                await emit_solo_status(session)
                await emit_confirmation(
                    session,
                    step_index=session.step_count + 1,
                    reason=assessment.reason,
                    action=str(confirm_action["action"]),
                    action_args=dict(confirm_action["action_args"]),
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
                    "screen_state": decision.screen_state,
                    "confidence": decision.confidence,
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

            # ━━ STEP 5: Execute one or more planned actions and assess outcome ━━
            for planned_offset, planned_action in enumerate(planned_actions):
                current_action = str(planned_action["action"])
                current_args = dict(planned_action["action_args"])
                is_primary_action = planned_offset == 0
                is_last_action = planned_offset == len(planned_actions) - 1
                current_decision = decision.model_copy(
                    update={
                        "action": current_action,
                        "action_args": current_args,
                        "batch_actions": [],
                    }
                )
                if not is_primary_action:
                    current_decision.thought_summary = (
                        f"{decision.thought_summary}\n[批处理] 连续执行第 "
                        f"{planned_offset + 1}/{len(planned_actions)} 个动作：{current_action}"
                    )
                    current_decision.agent_message = ""
                    current_decision.findings = []

                session.history.append(
                    {
                        "step": session.step_count + 1,
                        "decision": solo_service.decision_dict(current_decision),
                        "timestamp": utc_now(),
                    }
                )

                await emit_solo_step(
                    session,
                    step_index=session.step_count + 1,
                    action=current_action,
                    action_args=current_args,
                    thought_summary=current_decision.thought_summary,
                    expected_outcome=decision.progress or decision.expected_outcome,
                    agent_message=decision.agent_message if is_primary_action else None,
                    screenshot_path=screenshot_path,
                    findings=decision.findings if is_primary_action else [],
                    confidence=decision.confidence,
                    screen_state=decision.screen_state,
                )

                result = await execute_solo_step(
                    session,
                    current_action,
                    current_args,
                    capture_after=is_last_action,
                )
                if not is_solo_running(session):
                    return

                session.step_count += 1
                current_signature = record_solo_action_progress(
                    session,
                    current_action,
                    current_args,
                )

                new_screenshot = result.get("screenshot")
                if isinstance(new_screenshot, dict):
                    new_path = new_screenshot.get("path")
                    new_hash = new_screenshot.get("contentHash")
                    captured_at = new_screenshot.get("capturedAt")
                    if isinstance(new_path, str):
                        session.last_screenshot_path = new_path
                        screenshot_path = new_path
                    if isinstance(captured_at, str):
                        session.last_screenshot_at = captured_at
                    if isinstance(new_hash, str):
                        if new_hash == session.last_screenshot_hash:
                            session.same_screenshot_count += 1
                        else:
                            session.same_screenshot_count = 0
                        session.last_screenshot_hash = new_hash
                    await emit_solo_screenshot(
                        session,
                        new_screenshot,
                        f"第 {session.step_count} 步后截图",
                    )

                result_summary = summarize_solo_step_result(result)
                session.history[-1]["result"] = result_summary
                outcome = solo_kernel.assess_step(
                    current_decision,
                    result_summary,
                    repeat_action_count=session.repeat_action_count,
                    same_screenshot_count=session.same_screenshot_count,
                    repeat_action_signature_count=session.repeat_action_signature_count,
                )
                result_summary["outcomeClass"] = outcome.outcome_class
                result_summary["actionSignature"] = current_signature
                result_summary["repeatActionSignatureCount"] = session.repeat_action_signature_count
                if outcome.outcome_class == "no_op":
                    session.no_op_count += 1
                elif outcome.outcome_class == "uncertain":
                    session.uncertain_count += 1
                elif outcome.outcome_class == "failed":
                    session.failed_count += 1
                if outcome.recovery_hint:
                    session.recovery_mode_entries += 1
                if outcome.recovery_hint:
                    session.detail = f"SOLO 正在恢复: {outcome.recovery_hint}"
                solo_logger.write(
                    "action_result",
                    {
                        "step": session.step_count,
                        "action": current_action,
                        "batchIndex": planned_offset if planned_offset else None,
                        "result": result_summary,
                        "semanticSuccess": outcome.semantic_success,
                        "outcomeClass": outcome.outcome_class,
                        "recoveryHint": outcome.recovery_hint,
                    },
                )
                await emit_solo_trace(
                    session,
                    "step_result",
                    "completed" if outcome.semantic_success else "error",
                    f"视觉动作结果: {current_action}",
                    params={
                        "action": current_action,
                        "step": session.step_count,
                        "batchIndex": planned_offset if planned_offset else None,
                    },
                    result=result_summary,
                )
                if outcome.plan_changed:
                    await emit_solo_plan(session, solo_kernel)
                await emit_solo_status(session)

                if outcome.should_pause:
                    session.state = "paused"
                    session.detail = f"SOLO 连续恢复失败，已暂停: {outcome.pause_reason}"
                    solo_logger.write("paused", {"reason": session.detail, "action": current_action})
                    solo_logger.write("stability_summary", solo_stability_summary(session, "recovery_failed"))
                    await emit_solo_status(session)
                    return

                # ━━ STEP 6: Safety rails ━━
                if session.step_count >= session.max_steps:
                    session.state = "paused"
                    session.detail = f"超过最大步数 {session.max_steps}，已自动暂停。"
                    solo_logger.write("paused", {"reason": session.detail})
                    solo_logger.write("stability_summary", solo_stability_summary(session, "max_steps"))
                    await emit_solo_plan(session, solo_kernel)
                    await emit_solo_status(session)
                    return

            # ━━ STEP 7: Loop continues naturally (observe → think → act) ━━

    async def sync_runtime_config(next_config: AppConfig) -> None:
        runtime_state.update_config(next_config)
        solo_executor.set_preferred_display_index(next_config.solo.preferred_display_index)
        solo_executor._default_tools = build_default_tools(
            workspace_root=workspace_root,
            builtin_tools=[bt.model_dump() for bt in next_config.builtin_tools],
        )
        if im_bridge is not None:
            await im_bridge.update_config(next_config)

    async def stream_chat_reply(
        conversation_id: str,
        request_id: str,
        content: str,
        attachments: list[AttachmentRef] | None = None,
        preferred_mode: str | None = None,
    ) -> str:
        return await agent_runtime.handle_user_message(
            conversation_id,
            request_id,
            content,
            attachments=attachments,
            preferred_mode=preferred_mode,
        )

    async def handle_chat_from_im(
        binding: IMConversationBinding,
        content: str,
        request_id: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> str:
        return await stream_chat_reply(
            binding.conversation_id,
            request_id,
            content,
            attachments=attachments,
            preferred_mode="chat",
        )

    async def start_solo_for_conversation(
        conversation_id: str,
        task: str,
        request_id: str,
    ) -> str:
        nonlocal active_solo, solo_service, solo_kernel, solo_capabilities
        if active_solo is not None and active_solo.state in {
            "running",
            "paused",
            "waiting_user_confirmation",
        }:
            return "已有 SOLO 任务在进行中，请先 /stop 或等待它结束。"

        current_config = runtime_state.get_config()
        if not current_config.agent.vl_model_id or not current_config.agent.vl_api_key:
            return "SOLO 配置缺失，请先在 openEagle 设置中配置 VL 模型 ID 与 API Key。"

        await close_solo_capabilities()
        solo_capabilities = SoloCapabilityRuntime(
            current_config,
            workspace_root=workspace_root,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        solo_executor.set_preferred_display_index(current_config.solo.preferred_display_index)
        first_screenshot = await asyncio.to_thread(solo_tools.screenshot)
        slog(f"start request={request_id} conv={conversation_id} task={task[:120]}")
        active_solo = SoloSessionState(
            request_id=request_id,
            conversation_id=conversation_id,
            task=task,
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
        active_solo.log_path = solo_logger.start(request_id, task)
        capability_traces = await solo_capabilities.initialize()
        for trace in capability_traces:
            await emit_solo_trace(
                active_solo,
                trace.name,
                trace.status,
                trace.summary,
                params=trace.params,
                result=trace.result,
                kind=trace.kind,
            )
        solo_service = SoloService(current_config.agent, solo_capabilities)
        solo_kernel = SoloAgentKernel.create(task)
        await emit_solo_plan(active_solo, solo_kernel)
        await emit_solo_status(active_solo)
        if not active_solo.last_screenshot_path:
            active_solo.state = "error"
            active_solo.detail = "首帧截图失败，无法启动 SOLO。"
            await emit_solo_status(active_solo)
            await close_solo_capabilities()
            return active_solo.detail

        async def _start_agent() -> None:
            nonlocal solo_service
            if solo_service is None or active_solo is None or not is_solo_running(active_solo):
                return
            try:
                slog(
                    f"request={active_solo.request_id} agent_loop starting "
                    f"task={active_solo.task[:100]}"
                )
                solo_logger.write("agent_start", {"task": active_solo.task})
                await emit_solo_trace(
                    active_solo,
                    "agent",
                    "started",
                    "Agent 开始自主决策执行任务...",
                )
                await emit_solo_status(active_solo)
                await agent_loop(active_solo, active_solo.last_screenshot_path)
            except Exception as exc:  # noqa: BLE001
                if active_solo is None or not is_solo_running(active_solo):
                    return
                active_solo.state = "error"
                active_solo.detail = f"Agent 启动失败: {exc}"
                slog(f"request={active_solo.request_id} agent_start error={exc}")
                solo_logger.write("error", {"reason": active_solo.detail})
                await emit_solo_status(active_solo)
                await close_solo_capabilities()

        schedule_solo_task(_start_agent())
        return "收到，开始处理。"

    async def start_solo_from_im(
        binding: IMConversationBinding,
        task: str,
        request_id: str,
    ) -> str:
        return await stream_chat_reply(
            binding.conversation_id,
            request_id,
            task,
            preferred_mode="solo",
        )

    async def apply_solo_control(
        conversation_id: str,
        request_id: str,
        action: str,
    ) -> str:
        nonlocal active_solo
        if active_solo is None:
            return "当前没有进行中的 SOLO 任务。"
        if active_solo.conversation_id != conversation_id:
            return "当前 SOLO 任务属于另一个对话，不能从这里控制。"

        if action == "pause":
            active_solo.state = "paused"
            active_solo.detail = "用户已暂停 SOLO。"
            cancel_active_solo_task()
            solo_logger.write("paused", {"reason": active_solo.detail})
            await emit_solo_trace(active_solo, "control", "completed", "用户暂停 SOLO", params={"action": "pause"})
            await emit_solo_status(active_solo)
            return "SOLO 已暂停。"

        if action == "resume":
            if active_solo.last_screenshot_path is None:
                active_solo.state = "error"
                active_solo.detail = "缺少截图，无法恢复 SOLO。"
                solo_logger.write("error", {"reason": active_solo.detail})
                await emit_solo_status(active_solo)
                await close_solo_capabilities()
                return active_solo.detail
            active_solo.state = "running"
            active_solo.detail = "SOLO 已恢复。"
            await emit_solo_trace(active_solo, "control", "completed", "用户恢复 SOLO", params={"action": "resume"})
            await emit_solo_status(active_solo)
            schedule_solo_task(agent_loop(active_solo, active_solo.last_screenshot_path))
            return "SOLO 已恢复。"

        if action == "stop":
            active_solo.state = "aborted"
            active_solo.detail = "用户已结束 SOLO。"
            active_solo.completed_at = utc_now()
            active_solo.pending_confirmation = None
            cancel_active_solo_task()
            solo_logger.write("aborted", {"reason": active_solo.detail})
            await emit_solo_trace(active_solo, "control", "completed", "用户结束 SOLO", params={"action": "stop"})
            await emit_solo_status(active_solo)
            await close_solo_capabilities()
            return "SOLO 已结束。"

        if action == "confirm_reject":
            if not active_solo.pending_confirmation:
                return "没有待确认的 SOLO 动作。"
            active_solo.pending_confirmation = None
            active_solo.state = "paused"
            active_solo.detail = "用户拒绝了危险动作，SOLO 已暂停。"
            solo_logger.write("paused", {"reason": active_solo.detail})
            await emit_solo_status(active_solo)
            return "已拒绝危险动作，SOLO 已暂停。"

        if action != "confirm_allow":
            return f"不支持的 SOLO 控制动作: {action}"

        pending = active_solo.pending_confirmation
        if not pending:
            return "没有待确认的 SOLO 动作。"
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
            if active_solo is None:
                return
            result = await execute_solo_step(active_solo, str(pending["action"]), dict(pending["action_args"]))
            if active_solo is None or not is_solo_running(active_solo):
                return
            active_solo.step_count += 1
            action_str = str(pending["action"])
            record_solo_action_progress(
                active_solo,
                action_str,
                dict(pending["action_args"]),
            )

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
            next_screenshot = active_solo.last_screenshot_path or ""
            if next_screenshot:
                await agent_loop(active_solo, next_screenshot)

        schedule_solo_task(_execute_confirmed_and_continue())
        return "已允许危险动作，SOLO 会继续执行。"

    agent_runtime = AgentRuntime(
        config_getter=runtime_state.get_config,
        confirmation_store=tool_confirmations,
        attachment_store=attachment_store,
        confirmed_tool_results=confirmed_tool_results,
        send_event=safe_send,
        start_solo=start_solo_for_conversation,
        solo_control=apply_solo_control,
    )

    async def handle_tool_decision_from_im(
        conversation_id: str,
        request_id: str,
        decision: str,
    ) -> str:
        pending = tool_confirmations.latest_for_conversation(conversation_id)
        if pending is None:
            solo_action = "confirm_allow" if decision == "allow" else "confirm_reject"
            return await apply_solo_control(conversation_id, request_id, solo_action)

        pending = tool_confirmations.pop(pending.confirmation_id)
        if pending is None:
            return "待确认工具动作已过期。"
        if decision != "allow":
            await safe_send(
                "server:message",
                pending.request_id,
                pending.conversation_id,
                {"content": f"已拒绝执行工具动作：{pending.name}。"},
            )
            return f"已拒绝执行工具动作：{pending.name}。"

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
                attachment_store=attachment_store,
                attachment_request_id=request_id,
            )
            confirmed_tool_results[pending.conversation_id] = (
                f"上一轮确认执行的工具 `{pending.name}` 结果：\n{result}"
            )
            reply_attachments = attachment_store.peek_reply_attachments(
                pending.conversation_id,
                request_id,
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
            content = f"已执行确认动作 `{pending.name}`。\n\n```text\n{result}\n```"
            payload: dict[str, object] = {"content": content}
            if reply_attachments:
                payload["attachments"] = attachment_store.public_dicts(reply_attachments)
            await safe_send("server:message", pending.request_id, pending.conversation_id, payload)
            return content
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
            content = f"确认后的工具动作 `{pending.name}` 执行失败：{exc}"
            await safe_send("server:message", pending.request_id, pending.conversation_id, {"content": content})
            return content

    im_bridge = IMBridge(
        send_client=safe_send,
        handle_chat=handle_chat_from_im,
        start_solo=start_solo_from_im,
        solo_control=apply_solo_control,
        tool_decision=handle_tool_decision_from_im,
        attachment_store=attachment_store,
        reply_attachments=attachment_store.pop_reply_attachments,
    )
    bind_config_getter(im_bridge, runtime_state.get_config)
    await im_bridge.update_config(runtime_state.get_config())

    try:
        while True:
            raw = await websocket.receive_text()
            envelope = Envelope.model_validate_json(raw)

            if envelope.type == "client:update_settings":
                next_config = AppConfig.model_validate(envelope.payload["settings"])
                await sync_runtime_config(next_config)
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
                        attachment_store=attachment_store,
                    )
                    confirmed_tool_results[pending.conversation_id] = (
                        f"上一轮确认执行的工具 `{pending.name}` 结果：\n{result}"
                    )
                    reply_attachments = attachment_store.peek_reply_attachments(
                        pending.conversation_id,
                        pending.request_id,
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
                    payload: dict[str, object] = {
                        "content": f"已执行确认动作 `{pending.name}`。\n\n```text\n{result}\n```"
                    }
                    if reply_attachments:
                        payload["attachments"] = attachment_store.public_dicts(reply_attachments)
                    await safe_send(
                        "server:message",
                        pending.request_id,
                        pending.conversation_id,
                        payload,
                    )
                    if not pending.conversation_id.startswith("im_"):
                        attachment_store.pop_reply_attachments(
                            pending.conversation_id,
                            pending.request_id,
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
                solo_content = payload.content
                if payload.attachments:
                    try:
                        prepared_attachments = attachment_store.prepare_user_attachments(
                            envelope.conversation_id,
                            payload.attachments,
                        )
                    except AttachmentError as exc:
                        await safe_send(
                            "server:message",
                            envelope.request_id,
                            envelope.conversation_id,
                            {"content": f"附件处理失败: {exc}"},
                        )
                        continue
                    await safe_send(
                        "server:attachments_ready",
                        envelope.request_id,
                        envelope.conversation_id,
                        {"attachments": attachment_store.public_dicts(prepared_attachments)},
                    )
                    solo_content = append_attachment_context(solo_content, prepared_attachments)
                solo_reply = await start_solo_for_conversation(
                    envelope.conversation_id,
                    solo_content,
                    envelope.request_id,
                )
                if solo_reply != "收到，开始处理。":
                    await safe_send(
                        "server:message",
                        envelope.request_id,
                        envelope.conversation_id,
                        {"content": solo_reply},
                    )
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
                        await close_solo_capabilities()
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
                    await close_solo_capabilities()
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
                        record_solo_action_progress(
                            active_solo,
                            action_str,
                            dict(pending["action_args"]),
                        )

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
            await stream_chat_reply(
                envelope.conversation_id,
                envelope.request_id,
                payload.content,
                attachments=payload.attachments,
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
    finally:
        await close_solo_capabilities()
        if im_bridge is not None:
            try:
                await im_bridge.stop()
            except Exception:
                pass


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
            ws_max_size=ATTACHMENT_WS_MAX_SIZE,
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
