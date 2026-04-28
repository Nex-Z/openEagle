from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


INFORMATION_TASK_KEYWORDS = (
    "新闻",
    "资讯",
    "最近",
    "值得关注",
    "有哪些",
    "查询",
    "搜索",
    "搜一下",
    "查一下",
    "找一下",
    "资料",
    "信息",
    "天气",
    "价格",
    "汇率",
    "股票",
    "行情",
    "news",
    "latest",
    "recent",
    "search",
    "look up",
    "find out",
)

ARTIFACT_TASK_KEYWORDS = (
    "创建",
    "新建",
    "生成",
    "写入",
    "修改",
    "编辑",
    "保存",
    "删除",
    "移动",
    "复制",
    "安装",
    "构建",
    "运行测试",
    "文件",
    "目录",
    "项目",
    "代码",
    "文档",
    "表格",
    "图片",
    "create",
    "generate",
    "edit",
    "modify",
    "save",
    "delete",
    "install",
    "build",
    "test",
    "file",
)

OPERATION_TASK_KEYWORDS = (
    "打开",
    "启动",
    "运行",
    "点击",
    "输入",
    "填写",
    "切换",
    "登录",
    "关闭",
    "应用",
    "软件",
    "窗口",
    "open",
    "launch",
    "start",
    "click",
    "type",
    "login",
    "close",
)


@dataclass
class SoloPlanItem:
    index: int
    action: str
    description: str
    status: str = "pending"

    def to_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "description": self.description,
            "status": self.status,
        }


@dataclass
class SoloStepOutcome:
    semantic_success: bool
    should_pause: bool = False
    pause_reason: str | None = None
    recovery_hint: str | None = None
    plan_changed: bool = False


@dataclass
class SoloAgentKernel:
    task: str
    plan: list[SoloPlanItem] = field(default_factory=list)
    task_analysis: str = ""
    alternative: str = ""
    agent_message: str = ""
    replan_count: int = 0
    findings: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    repeated_action_recoveries: int = 0
    stalled_screen_recoveries: int = 0
    last_recovery_hint: str = ""
    max_consecutive_failures: int = 4
    max_stall_recoveries: int = 3

    @classmethod
    def create(cls, task: str) -> "SoloAgentKernel":
        kernel = cls(task=task)
        kernel.task_analysis = kernel._infer_task_analysis(task)
        kernel.plan = [
            SoloPlanItem(1, "observe", "观察当前屏幕，确认起点和干扰项。", "in_progress"),
            SoloPlanItem(2, "route", "选择最快路径：命令行、应用启动、网页或直接 GUI 操作。"),
            SoloPlanItem(3, "execute", "执行下一步并验证结果，不原样重复失败动作。"),
            SoloPlanItem(4, "report", "整理发现和完成状态，向用户汇报。"),
        ]
        kernel.agent_message = "SOLO 已接管任务，会边观察边执行，并在失败时自动换路线。"
        return kernel

    @staticmethod
    def _infer_task_analysis(task: str) -> str:
        task_lower = task.lower()
        if any(keyword in task_lower for keyword in ["搜索", "查询", "新闻", "天气", "价格", "网页", "浏览器", "http"]):
            return "信息查询或网页操作任务：优先用可靠入口打开页面，提取对用户有用的信息并汇报。"
        if any(keyword in task_lower for keyword in ARTIFACT_TASK_KEYWORDS):
            return "产物交付任务：执行后确认文件、内容、构建、安装或测试结果可复核。"
        if any(keyword in task_lower for keyword in ["打开", "启动", "运行", "应用", "软件"]):
            return "应用操作任务：优先用命令或系统启动入口打开目标，再用截图验证。"
        if any(keyword in task_lower for keyword in ["填写", "输入", "点击", "保存", "提交"]):
            return "GUI 操作任务：先定位目标控件，再小步执行并验证屏幕变化。"
        return "通用视觉桌面任务：持续观察、执行、验证，必要时重新规划。"

    def plan_payload(self) -> dict[str, Any]:
        return {
            "items": [item.to_payload() for item in self.plan],
            "taskAnalysis": self.task_analysis,
            "alternative": self.alternative,
            "agentMessage": self.agent_message,
            "replanCount": self.replan_count,
        }

    def prompt_context(self) -> dict[str, Any]:
        requirement = self.completion_requirement()
        return {
            "taskAnalysis": self.task_analysis,
            "plan": [item.to_payload() for item in self.plan],
            "findings": self.findings[-20:],
            "completionMode": requirement["mode"],
            "completionRequirement": requirement["description"],
            "requiresFindings": requirement["mode"] == "information",
            "consecutiveFailures": self.consecutive_failures,
            "lastRecoveryHint": self.last_recovery_hint,
            "replanCount": self.replan_count,
        }

    def requires_findings(self) -> bool:
        return self.completion_mode() == "information"

    def completion_mode(self) -> str:
        task_lower = self.task.lower()
        if any(keyword in task_lower for keyword in INFORMATION_TASK_KEYWORDS):
            return "information"
        if any(keyword in task_lower for keyword in ARTIFACT_TASK_KEYWORDS):
            return "artifact"
        if any(keyword in task_lower for keyword in OPERATION_TASK_KEYWORDS):
            return "operation"
        return "general"

    def completion_requirement(self) -> dict[str, str]:
        mode = self.completion_mode()
        descriptions = {
            "information": "必须交付具体答案：findings 或 finish_report 里要有可用信息；screen_state、progress 或只说页面状态不算完成。",
            "artifact": "必须说明产物或验证结果：文件、内容、安装、构建或测试结果要可复核。",
            "operation": "必须说明可见完成状态：目标窗口、控件状态或执行结果已经达到用户要求。",
            "general": "必须给出可复核的完成证据：屏幕状态、执行结果、findings 或最终汇报不能为空泛。",
        }
        return {"mode": mode, "description": descriptions[mode]}

    def has_completion_evidence(self, session_findings: list[str], decision: Any) -> bool:
        mode = self.completion_mode()
        if self._has_findings(session_findings, decision):
            return True
        if mode == "information":
            return self._has_answer_like_finish_report(decision)
        return self._has_substantive_finish_text(decision)

    def completion_block_reason(self) -> str:
        requirement = self.completion_requirement()
        return (
            f"完成证据不足：{requirement['description']} "
            "请继续观察、执行、等待、滚动、读取结果或换路线，直到可以给用户一个有用交代。"
        )

    @staticmethod
    def _has_findings(session_findings: list[str], decision: Any) -> bool:
        if any(str(finding).strip() for finding in session_findings):
            return True
        decision_findings = getattr(decision, "findings", []) or []
        return any(str(finding).strip() for finding in decision_findings)

    @staticmethod
    def _finish_text(decision: Any) -> str:
        parts = [
            getattr(decision, "finish_report", ""),
            getattr(decision, "agent_message", ""),
            getattr(decision, "progress", ""),
            getattr(decision, "screen_state", ""),
        ]
        return " ".join(str(part).strip() for part in parts if str(part).strip())

    @classmethod
    def _has_substantive_finish_text(cls, decision: Any) -> bool:
        text = cls._finish_text(decision)
        normalized = text.strip().lower().replace("。", "").replace(".", "")
        weak_exact = {"完成", "已完成", "好了", "ok", "done", "success", "成功"}
        if normalized in weak_exact:
            return False
        return len(normalized) >= 6

    @staticmethod
    def _has_answer_like_finish_report(decision: Any) -> bool:
        text = str(getattr(decision, "finish_report", "") or "").strip()
        normalized = text.strip().lower()
        if len(normalized) < 40:
            return False
        weak_page_only = (
            "搜索结果页",
            "搜索结果页面",
            "打开搜索",
            "打开网页",
            "页面已打开",
            "已打开页面",
            "空白加载",
            "正在加载",
            "正在搜索",
            "准备搜索",
            "提交搜索",
            "搜索请求",
            "搜索成功",
        )
        if any(phrase in normalized for phrase in weak_page_only):
            return False
        return any(marker in normalized for marker in ("：", ":", "1.", "1、", "2.", "2、", "-", "包括", "如下", "\n"))

    def reject_premature_finish(self, reason: str) -> bool:
        self.last_recovery_hint = reason
        self.alternative = reason
        self.agent_message = "完成证据还不够，SOLO 将继续观察、执行并验证结果。"
        self.replan_count += 1
        execute_item = next((item for item in self.plan if item.action == "execute"), None)
        if execute_item:
            execute_item.status = "in_progress"
            execute_item.description = "继续获取可复核的完成证据，再整理汇报。"
        report_item = next((item for item in self.plan if item.action == "report"), None)
        if report_item and report_item.status == "completed":
            report_item.status = "pending"
        return True

    def record_decision(self, decision: Any) -> bool:
        changed = False
        findings = getattr(decision, "findings", []) or []
        for finding in findings:
            text = str(finding).strip()
            if text and text not in self.findings:
                self.findings.append(text)

        updates = getattr(decision, "plan_updates", []) or []
        if self._apply_plan_updates(updates):
            changed = True

        if getattr(decision, "action", "") == "finish":
            for item in self.plan:
                if item.status in {"pending", "in_progress"}:
                    item.status = "completed"
            self.agent_message = getattr(decision, "agent_message", "") or "SOLO 准备收尾汇报。"
            changed = True
        else:
            self._ensure_active_plan_item()

        return changed

    def assess_step(
        self,
        decision: Any,
        result_summary: dict[str, Any],
        repeat_action_count: int,
        same_screenshot_count: int,
    ) -> SoloStepOutcome:
        semantic_success = self._result_semantically_successful(result_summary)
        recovery_hint = ""
        plan_changed = False

        if semantic_success:
            self.consecutive_failures = 0
            self.last_recovery_hint = ""
            plan_changed = self._mark_progress_after_success(getattr(decision, "action", ""))
        else:
            self.consecutive_failures += 1
            recovery_hint = self._build_failure_recovery_hint(decision, result_summary)
            plan_changed = self._mark_recovery(recovery_hint)

        if repeat_action_count >= 3:
            self.repeated_action_recoveries += 1
            recovery_hint = (
                "同一动作已经连续重复。请换路线：重新观察目标位置、改用命令行或先切换窗口，"
                "不要原样重复上一步。"
            )
            plan_changed = self._mark_recovery(recovery_hint) or plan_changed

        if same_screenshot_count >= 2:
            self.stalled_screen_recoveries += 1
            recovery_hint = (
                "连续截图几乎没有变化。请判断是否点错、窗口未聚焦、页面未加载或需要滚动/切换应用，"
                "下一步必须改变策略。"
            )
            plan_changed = self._mark_recovery(recovery_hint) or plan_changed

        should_pause = (
            self.consecutive_failures >= self.max_consecutive_failures
            or self.repeated_action_recoveries > self.max_stall_recoveries
            or self.stalled_screen_recoveries > self.max_stall_recoveries
        )
        pause_reason = None
        if should_pause:
            pause_reason = recovery_hint or "SOLO 连续恢复失败，已暂停等待人工判断。"

        return SoloStepOutcome(
            semantic_success=semantic_success,
            should_pause=should_pause,
            pause_reason=pause_reason,
            recovery_hint=recovery_hint or None,
            plan_changed=plan_changed,
        )

    def _apply_plan_updates(self, updates: object) -> bool:
        if not isinstance(updates, list):
            return False
        changed = False
        by_index = {item.index: item for item in self.plan}
        valid_status = {"pending", "in_progress", "completed", "failed", "skipped"}
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                index = int(update.get("index"))
            except (TypeError, ValueError):
                continue
            item = by_index.get(index)
            if item is None:
                action = str(update.get("action") or "step")
                description = str(update.get("description") or action)
                status = str(update.get("status") or "pending")
                if status not in valid_status:
                    status = "pending"
                self.plan.append(SoloPlanItem(index, action, description, status))
                changed = True
                continue
            status = str(update.get("status") or item.status)
            if status in valid_status and status != item.status:
                item.status = status
                changed = True
            description = str(update.get("description") or "")
            if description and description != item.description:
                item.description = description
                changed = True
            action = str(update.get("action") or "")
            if action and action != item.action:
                item.action = action
                changed = True
        if changed:
            self.plan.sort(key=lambda item: item.index)
        return changed

    def _ensure_active_plan_item(self) -> None:
        if any(item.status == "in_progress" for item in self.plan):
            return
        for item in self.plan:
            if item.status == "pending":
                item.status = "in_progress"
                return

    def _mark_progress_after_success(self, action: str) -> bool:
        changed = False
        active = next((item for item in self.plan if item.status == "in_progress"), None)
        if active and active.action in {"observe", "route"}:
            active.status = "completed"
            changed = True
        if active and active.action == "execute" and action in {"finish"}:
            active.status = "completed"
            changed = True
        if action not in {"screenshot", "wait"}:
            execute_item = next((item for item in self.plan if item.action == "execute"), None)
            if execute_item and execute_item.status == "pending":
                execute_item.status = "in_progress"
                changed = True
        self._ensure_active_plan_item()
        return changed

    def _mark_recovery(self, recovery_hint: str) -> bool:
        self.last_recovery_hint = recovery_hint
        self.replan_count += 1
        self.alternative = recovery_hint
        self.agent_message = "检测到执行没有按预期推进，SOLO 将换路线继续尝试。"
        execute_item = next((item for item in self.plan if item.action == "execute"), None)
        if execute_item:
            execute_item.status = "in_progress"
            execute_item.description = "按恢复提示换路线执行，并验证结果。"
        return True

    @staticmethod
    def _result_semantically_successful(result_summary: dict[str, Any]) -> bool:
        if not result_summary.get("success", False):
            return False
        if result_summary.get("ok") is False:
            return False
        if result_summary.get("exitCode") not in (None, 0):
            return False
        for key in ("error", "executionError"):
            if result_summary.get(key):
                return False
        output_tail = str(result_summary.get("outputTail", ""))
        if output_tail.startswith("[TIMEOUT]"):
            return False
        return True

    @staticmethod
    def _build_failure_recovery_hint(decision: Any, result_summary: dict[str, Any]) -> str:
        action = str(getattr(decision, "action", "unknown"))
        if action == "execute_command":
            return (
                "上一步命令执行失败。请阅读 exitCode/outputTail，修正命令、换更短的命令，"
                "或改用 GUI 路线；不要原样重复失败命令。"
            )
        if result_summary.get("executionError") or result_summary.get("error"):
            return "上一步动作执行异常。请先截图确认当前状态，再选择更稳妥的动作。"
        return "上一步没有达到预期。请重新观察屏幕，换一个能推进任务的策略。"
