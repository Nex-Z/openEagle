from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, ContextConfig
from .context_cleanup import compact_messages_for_prompt_with_ai
from .langgraph_agent import run_text_model
from .memory import store
from .memory.models import ConversationTurnPayload

ConfigGetter = Callable[[], AppConfig]

DEFAULT_CONTEXT_BUDGET = 12_000
MIN_CONTEXT_BUDGET = 2_000
FALLBACK_SUMMARY_CHARS = 2_400


@dataclass(frozen=True)
class ConversationContextWindow:
    text: str
    turn_count: int
    compacted: bool
    method: str


class ConversationContextService:
    def __init__(self, config_getter: ConfigGetter) -> None:
        self._config_getter = config_getter
        self._locks: dict[str, asyncio.Lock] = {}

    async def seed_from_history(
        self,
        conversation_id: str,
        history: list[dict[str, Any]] | None,
    ) -> None:
        if not history or store.list_conversation_turns(conversation_id):
            return
        async with self._lock(conversation_id):
            if store.list_conversation_turns(conversation_id):
                return
            turns = self._history_turns(history)
            for index, turn in enumerate(turns, start=1):
                store.upsert_conversation_turn(
                    conversation_id=conversation_id,
                    request_id=turn["request_id"] or f"seed-{index}",
                    user_content=turn["user_content"],
                    assistant_content=turn["assistant_content"],
                    route="client_seed",
                    metadata={"source": "client_history"},
                    created_at=turn["created_at"],
                )
            await self._enforce_retention_locked(conversation_id)

    async def record_turn(
        self,
        *,
        conversation_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
        route: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock(conversation_id):
            store.upsert_conversation_turn(
                conversation_id=conversation_id,
                request_id=request_id,
                user_content=user_content.strip(),
                assistant_content=assistant_content.strip(),
                route=route,
                metadata=metadata,
            )
            await self._enforce_retention_locked(conversation_id)

    async def context_for_prompt(self, conversation_id: str) -> ConversationContextWindow:
        async with self._lock(conversation_id):
            await self._bootstrap_from_memory_events_locked(conversation_id)
            turns = store.list_conversation_turns(conversation_id)
            state = store.get_conversation_context_state(conversation_id)

        if not turns and not state.archive_summary and not state.idle_summary:
            return ConversationContextWindow("", 0, False, "none")

        if state.idle_summary:
            summary = state.idle_summary
            visible_turns = [
                turn for turn in turns if turn.id > state.idle_through_turn_id
            ]
        else:
            summary = state.archive_summary
            visible_turns = turns

        messages = self._messages(summary, visible_turns)
        context_config = self._context_budget_config(self._config_getter().context)
        cleanup = await compact_messages_for_prompt_with_ai(
            messages,
            context_config,
            summarizer=self._summarize,
        )
        return ConversationContextWindow(
            text=self._render_messages(cleanup.messages),
            turn_count=len(turns),
            compacted=cleanup.changed,
            method=cleanup.method,
        )

    async def compact_for_idle(self, conversation_id: str) -> bool:
        async with self._lock(conversation_id):
            await self._bootstrap_from_memory_events_locked(conversation_id)
            turns = store.list_conversation_turns(conversation_id)
            if not turns:
                return False

            config = self._config_getter().context
            recent_turns = max(1, math.ceil(max(0, config.preserve_recent_messages) / 2))
            eligible = turns[:-recent_turns] if len(turns) > recent_turns else []
            if not eligible:
                return False

            state = store.get_conversation_context_state(conversation_id)
            through_turn_id = eligible[-1].id
            if state.idle_summary and state.idle_through_turn_id >= through_turn_id:
                return False

            summary = await self._merge_summary(
                state.archive_summary,
                eligible,
                reason="远程 IM 会话进入静默期",
            )
            store.update_conversation_context_state(
                conversation_id,
                idle_summary=summary,
                idle_through_turn_id=through_turn_id,
            )
            return True

    async def _enforce_retention_locked(self, conversation_id: str) -> None:
        await self._bootstrap_from_memory_events_locked(conversation_id)
        turns = store.list_conversation_turns(conversation_id)
        limit = max(1, self._config_getter().context.conversation_turn_limit)
        if len(turns) <= limit:
            return

        overflow = turns[:-limit]
        state = store.get_conversation_context_state(conversation_id)
        archive_summary = await self._merge_summary(
            state.archive_summary,
            overflow,
            reason=f"会话完整轮次超过 {limit} 轮",
        )
        store.update_conversation_context_state(
            conversation_id,
            archive_summary=archive_summary,
            idle_summary="",
            idle_through_turn_id=0,
        )
        store.delete_conversation_turns(
            conversation_id,
            [turn.id for turn in overflow],
        )

    async def _bootstrap_from_memory_events_locked(self, conversation_id: str) -> None:
        if store.list_conversation_turns(conversation_id):
            return
        limit = max(1, self._config_getter().context.conversation_turn_limit)
        events = store.list_conversation_events(
            conversation_id,
            source="turn",
            limit=limit,
        )
        for event in events:
            user_content, assistant_content = self._parse_memory_turn(event.content)
            if not user_content and not assistant_content:
                continue
            store.upsert_conversation_turn(
                conversation_id=conversation_id,
                request_id=event.request_id or event.id,
                user_content=user_content,
                assistant_content=assistant_content,
                route=str(event.payload.get("route") or "memory_bootstrap"),
                metadata={"source": "memory_event", **event.payload},
                created_at=event.created_at,
            )

    async def _merge_summary(
        self,
        existing_summary: str,
        turns: list[ConversationTurnPayload],
        *,
        reason: str,
    ) -> str:
        source = self._summary_source(existing_summary, turns)
        prompt = (
            "你是 openEagle 的会话上下文压缩器。请把下面内容压缩成可供未来继续对话的中文摘要。\n"
            "必须保留用户正在讨论的对象、明确目标、偏好、约束、已作决定、关键结论、"
            "未完成事项，以及代词或“继续/刚才那个”所依赖的指代关系。"
            "工具调用仅保留工具名称、关键观察和最终结果，不保留参数原文、日志或大段输出。"
            "删除寒暄、重复表述和无关过程，不要编造。\n"
            f"压缩原因：{reason}\n"
            f"长度上限约 {max(400, self._config_getter().context.summary_char_limit)} 字。\n\n"
            f"{source}"
        )
        summary = await self._summarize(prompt)
        if summary:
            return self._truncate(
                summary,
                self._config_getter().context.summary_char_limit,
            )
        return self._fallback_summary(existing_summary, turns)

    async def _summarize(self, prompt: str) -> str:
        config = self._config_getter()
        agent = config.agent
        if not config.context.ai_summary_enabled or not agent.api_key:
            return ""
        try:
            if agent.provider == "anthropic":
                import anthropic

                client = anthropic.AsyncAnthropic(api_key=agent.api_key)
                response = await client.messages.create(
                    model=agent.model_id or "claude-sonnet-4-20250514",
                    max_tokens=2048,
                    system="你只负责压缩会话上下文，输出摘要正文。",
                    messages=[{"role": "user", "content": prompt}],
                )
                return "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
            if agent.provider in {"openai", "openai-like"}:
                return (
                    await run_text_model(
                        agent_config=agent,
                        instructions=["你只负责压缩会话上下文，输出摘要正文。"],
                        prompt=prompt,
                    )
                ).strip()
        except Exception:
            return ""
        return ""

    def _context_budget_config(self, config: ContextConfig) -> ContextConfig:
        budget = min(
            DEFAULT_CONTEXT_BUDGET,
            max(MIN_CONTEXT_BUDGET, config.max_input_tokens // 2),
        )
        return config.model_copy(update={"max_input_tokens": budget})

    @staticmethod
    def _messages(
        summary: str,
        turns: list[ConversationTurnPayload],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if summary.strip():
            messages.append(
                {
                    "role": "system",
                    "content": "【更早的会话摘要】\n" + summary.strip(),
                }
            )
        for turn in turns:
            if turn.user_content.strip():
                messages.append({"role": "user", "content": turn.user_content.strip()})
            if turn.assistant_content.strip():
                messages.append(
                    {"role": "assistant", "content": turn.assistant_content.strip()}
                )
        return messages

    @staticmethod
    def _render_messages(messages: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for message in messages:
            role = str(message.get("role") or "")
            label = {
                "system": "会话摘要",
                "user": "用户",
                "assistant": "助手",
            }.get(role, role or "上下文")
            content = str(message.get("content") or "").strip()
            if content:
                rows.append(f"{label}: {content}")
        return "\n".join(rows)

    @classmethod
    def _summary_source(
        cls,
        existing_summary: str,
        turns: list[ConversationTurnPayload],
    ) -> str:
        rows: list[str] = []
        if existing_summary.strip():
            rows.append("已有更早摘要:\n" + existing_summary.strip())
        for turn in turns:
            rows.append(
                "\n".join(
                    part
                    for part in (
                        f"用户: {cls._truncate(turn.user_content, 800)}",
                        f"助手: {cls._truncate(turn.assistant_content, 800)}",
                    )
                    if part.split(": ", 1)[-1].strip()
                )
            )
        return "\n\n".join(row for row in rows if row)

    def _fallback_summary(
        self,
        existing_summary: str,
        turns: list[ConversationTurnPayload],
    ) -> str:
        rows = [existing_summary.strip()] if existing_summary.strip() else []
        for turn in turns:
            user = self._truncate(turn.user_content, 260)
            assistant = self._truncate(turn.assistant_content, 260)
            if user:
                rows.append(f"用户谈到：{user}")
            if assistant:
                rows.append(f"助手结论：{assistant}")
        limit = max(
            400,
            self._config_getter().context.summary_char_limit
            or FALLBACK_SUMMARY_CHARS,
        )
        return self._truncate("\n".join(rows), limit, keep_tail=True)

    @staticmethod
    def _parse_memory_turn(content: str) -> tuple[str, str]:
        match = re.match(
            r"\s*用户:\s*\n?(.*?)\n\n助手:\s*\n?(.*)\s*$",
            content,
            re.S,
        )
        if not match:
            return "", ""
        return match.group(1).strip(), match.group(2).strip()

    @staticmethod
    def _history_turns(history: list[dict[str, Any]]) -> list[dict[str, str]]:
        turns: list[dict[str, str]] = []
        pending: dict[str, str] | None = None
        for item in history:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            if role == "user":
                if pending and pending["user_content"]:
                    turns.append(pending)
                pending = {
                    "request_id": str(item.get("requestId") or item.get("id") or ""),
                    "user_content": content,
                    "assistant_content": "",
                    "created_at": str(item.get("createdAt") or ""),
                }
                continue
            if pending is None:
                continue
            pending["assistant_content"] = content
            turns.append(pending)
            pending = None
        if pending and pending["user_content"]:
            turns.append(pending)
        return turns

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    @staticmethod
    def _truncate(
        text: str,
        limit: int,
        *,
        keep_tail: bool = False,
    ) -> str:
        clean = text.strip()
        max_chars = max(1, int(limit))
        if len(clean) <= max_chars:
            return clean
        if keep_tail:
            return "…\n" + clean[-max_chars:]
        return clean[:max_chars].rstrip() + "\n…"
