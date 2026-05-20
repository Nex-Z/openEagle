from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ..config import AgentConfig, AppConfig
from .models import MemoryNotePayload, MemoryStatePayload
from . import store


ConfigGetter = Callable[[], AppConfig]

RAW_CONTENT_LIMIT = 24_000
RAW_PAYLOAD_LIMIT = 12_000
PROMPT_CONTEXT_LIMIT = 6_000
MAX_PROMPT_NOTES = 14
REDACTED_SECRET = "[redacted-secret]"

SECRET_KEY_PATTERN = (
    r"api[_-]?key|apikey|token|secret|password|authorization|"
    r"app[_-]?secret|appsecret|bot[_-]?token|bottoken|"
    r"verification[_-]?token|verificationtoken"
)
SECRET_KEY_VALUE_PATTERNS = (
    re.compile(
        rf"(?i)([\"']?\b(?:{SECRET_KEY_PATTERN})\b[\"']?\s*[:=]\s*)([\"'])[^\"']*(\2)"
    ),
    re.compile(
        rf"(?i)(\b(?:{SECRET_KEY_PATTERN})\b\s*[:=]\s*)[^'\"\s,;}}]+"
    ),
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    suffix = f"\n...[truncated {omitted} chars]"
    head_limit = max(0, limit - len(suffix))
    return f"{text[:head_limit]}{suffix}"


def sanitize_text(text: str, limit: int = RAW_CONTENT_LIMIT) -> str:
    cleaned = text
    cleaned = SECRET_KEY_VALUE_PATTERNS[0].sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{REDACTED_SECRET}{match.group(3)}"
        ),
        cleaned,
    )
    cleaned = SECRET_KEY_VALUE_PATTERNS[1].sub(
        lambda match: f"{match.group(1)}{REDACTED_SECRET}",
        cleaned,
    )
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub(REDACTED_SECRET, cleaned)
    return _truncate(cleaned, limit)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        "password" in normalized
        or "secret" in normalized
        or "token" in normalized
        or "apikey" in normalized
        or normalized == "authorization"
    )


def _sanitize_structured(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[truncated-depth]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            cleaned[key_text] = (
                REDACTED_SECRET
                if _is_sensitive_key(key_text)
                else _sanitize_structured(item, depth + 1)
            )
        return cleaned
    if isinstance(value, list):
        return [_sanitize_structured(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_structured(item, depth + 1) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _jsonable_preview(value: Any, limit: int = RAW_PAYLOAD_LIMIT) -> dict[str, Any]:
    cleaned_value = _sanitize_structured(value)
    try:
        text = json.dumps(cleaned_value, ensure_ascii=False)
    except Exception:
        text = str(cleaned_value)
    text = sanitize_text(text, limit)
    try:
        payload = json.loads(text)
    except Exception:
        payload = {"preview": text}
    return payload if isinstance(payload, dict) else {"value": payload}


def _is_auto_source(source: str) -> bool:
    return source.startswith("auto")


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        candidate = text[match.start() :]
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("memory distillation output does not contain JSON")


class MemoryService:
    def __init__(self, *, config_getter: ConfigGetter | None = None) -> None:
        self._config_getter = config_getter

    def state(self) -> MemoryStatePayload:
        return store.get_state()

    def state_payload(self) -> dict[str, Any]:
        return self.state().model_dump(by_alias=True, exclude_none=True)

    def prompt_context(self, max_chars: int = PROMPT_CONTEXT_LIMIT) -> str:
        state = store.get_state(include_archived=False, audit_limit=0, event_limit=0)
        sections: list[str] = []
        profile = state.profile.content.strip()
        if profile:
            sections.append("用户画像:\n" + profile)

        active_notes = [note for note in state.notes if note.status == "active" and note.text.strip()]
        if active_notes:
            lines = []
            for note in active_notes[:MAX_PROMPT_NOTES]:
                tags = f" [{', '.join(note.tags)}]" if note.tags else ""
                lines.append(f"-{tags} {note.text.strip()}")
            sections.append("用户笔记:\n" + "\n".join(lines))

        soul_parts: list[str] = []
        if state.agent_soul.core.strip():
            soul_parts.append("核心人格:\n" + state.agent_soul.core.strip())
        if state.agent_soul.side_notes.strip():
            soul_parts.append("Agent 自动旁注:\n" + state.agent_soul.side_notes.strip())
        if soul_parts:
            sections.append("Agent 个性灵魂:\n" + "\n\n".join(soul_parts))

        if not sections:
            return ""
        text = (
            "长期记忆上下文。用于保持对用户、笔记和 Agent 个性的连续理解；"
            "不要向用户暴露内部字段或审计细节。\n\n"
            + "\n\n".join(sections)
        )
        return _truncate(text, max_chars)

    def save_manual(self, payload: dict[str, Any]) -> None:
        profile = payload.get("profile")
        if isinstance(profile, dict) and isinstance(profile.get("content"), str):
            store.update_profile(profile["content"], source="manual", manual=True)

        agent_soul = payload.get("agentSoul") or payload.get("agent_soul")
        if isinstance(agent_soul, dict):
            core = agent_soul.get("core")
            if isinstance(core, str):
                store.update_agent_soul(core=core, source="manual")

        notes = payload.get("notes")
        if isinstance(notes, list):
            existing_notes = {note.id: note for note in store.get_state().notes}
            for raw_note in notes:
                if not isinstance(raw_note, dict):
                    continue
                text = str(raw_note.get("text") or "").strip()
                if not text:
                    continue
                note_id = str(raw_note.get("id") or "").strip()
                existing = existing_notes.get(note_id)
                tags = _normalize_tags(raw_note.get("tags"))
                confidence = self._safe_confidence(
                    raw_note.get(
                        "confidence",
                        existing.confidence if existing is not None else 1.0,
                    )
                )
                status = (
                    "archived"
                    if str(raw_note.get("status") or "active") == "archived"
                    else "active"
                )
                source = existing.source if existing is not None else "manual"
                if existing is None or self._note_changed(
                    existing,
                    text=text,
                    tags=tags,
                    confidence=confidence,
                    status=status,
                ):
                    source = "manual"
                store.upsert_note(
                    MemoryNotePayload.model_validate(
                        {
                            **raw_note,
                            "text": text,
                            "tags": tags,
                            "confidence": confidence,
                            "status": status,
                            "source": source,
                        }
                    ),
                    source="manual",
                )

    def ingest_snapshot(
        self,
        *,
        conversation_id: str,
        request_id: str,
        source: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        safe_content = sanitize_text(content)
        summary = f"{source} 记忆快照"
        return store.append_event(
            source=source,
            conversation_id=conversation_id,
            request_id=request_id,
            summary=summary,
            content=safe_content,
            payload=_jsonable_preview(payload or {}),
        )

    def record_turn(
        self,
        *,
        conversation_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
        route: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        content = sanitize_text(
            "\n\n".join(
                part
                for part in (
                    f"用户:\n{user_content.strip()}",
                    f"助手:\n{assistant_content.strip()}",
                )
                if part.strip()
            )
        )
        return store.append_event(
            source="turn",
            conversation_id=conversation_id,
            request_id=request_id,
            summary=f"用户回合完成{f' ({route})' if route else ''}",
            content=content,
            payload=_jsonable_preview(metadata or {}),
        )

    async def distill_event(self, event_id: str) -> bool:
        event = store.get_event(event_id)
        if event is None or not self._config_getter:
            return False
        config = self._config_getter()
        if not self._can_distill(config.agent):
            return False
        prompt = self._build_distillation_prompt(
            store.get_state(),
            event.model_dump(by_alias=True),
        )
        try:
            raw = await self._distill_with_model(config.agent, prompt)
            payload = _extract_json(raw)
        except Exception:
            return False
        return self.apply_distillation(payload, source=f"auto:{event.id}")

    def apply_distillation(self, payload: dict[str, Any], *, source: str = "auto") -> bool:
        changed = False
        current_state = (
            store.get_state(audit_limit=0, event_limit=0) if _is_auto_source(source) else None
        )
        manual_profile_locked = bool(current_state and current_state.profile.manual_updated_at)
        manual_note_ids = {
            note.id
            for note in (current_state.notes if current_state else [])
            if note.source == "manual"
        }
        profile = payload.get("profile")
        if manual_profile_locked:
            pass
        elif isinstance(profile, str) and profile.strip():
            store.update_profile(profile.strip(), source=source)
            changed = True
        elif isinstance(profile, dict):
            content = profile.get("content")
            if isinstance(content, str) and content.strip():
                store.update_profile(content.strip(), source=source)
                changed = True

        agent_side_notes = (
            payload.get("agentSideNotes")
            or payload.get("agent_side_notes")
            or payload.get("sideNotes")
        )
        if isinstance(agent_side_notes, str) and agent_side_notes.strip():
            store.update_agent_soul(side_notes=agent_side_notes.strip(), source=source)
            changed = True

        notes = payload.get("notes")
        if isinstance(notes, list):
            for raw_note in notes:
                if not isinstance(raw_note, dict):
                    continue
                action = str(raw_note.get("action") or "add").lower()
                note_id = str(raw_note.get("id") or "").strip()
                if note_id and note_id in manual_note_ids:
                    continue
                if action == "archive" and note_id:
                    changed = store.archive_note(
                        note_id,
                        source=source,
                        reason=str(raw_note.get("reason") or "自动记忆蒸馏归档。"),
                    ) or changed
                    continue
                text = str(raw_note.get("text") or "").strip()
                if not text:
                    continue
                note_payload: dict[str, Any] = {
                    "text": text,
                    "tags": _normalize_tags(raw_note.get("tags")),
                    "source": source,
                    "confidence": self._safe_confidence(raw_note.get("confidence")),
                    "status": "archived"
                    if str(raw_note.get("status") or "active") == "archived"
                    else "active",
                }
                if note_id:
                    note_payload["id"] = note_id
                store.upsert_note(
                    MemoryNotePayload.model_validate(note_payload),
                    source=source,
                )
                changed = True
        return changed

    @staticmethod
    def _note_changed(
        existing: MemoryNotePayload,
        *,
        text: str,
        tags: list[str],
        confidence: float,
        status: str,
    ) -> bool:
        return (
            existing.text.strip() != text
            or existing.tags != tags
            or abs(float(existing.confidence) - confidence) > 0.0001
            or existing.status != status
        )

    @staticmethod
    def _safe_confidence(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.7
        return min(max(number, 0.0), 1.0)

    @staticmethod
    def _can_distill(agent_config: AgentConfig) -> bool:
        return agent_config.provider in {"openai", "openai-like", "anthropic"} and bool(
            agent_config.api_key
        )

    @staticmethod
    def _build_distillation_prompt(state: MemoryStatePayload, event: dict[str, Any]) -> str:
        current = state.model_dump(by_alias=True, exclude_none=True)
        compact_state = {
            "profile": current.get("profile", {}),
            "notes": current.get("notes", [])[:30],
            "agentSoul": current.get("agentSoul", {}),
        }
        return (
            "你是 openEagle 的长期记忆整理器。请根据新的原始记忆事件更新记忆。\n"
            "目标不是只挑最有价值的内容：原始事件已经保存；你的任务是把其中对未来对话有帮助的内容蒸馏进画像、笔记或 Agent 旁注。\n"
            "不要保存明显的一次性寒暄、临时验证码、密钥、token、密码或完整敏感凭据。\n"
            "用户画像应是完整改写后的 markdown 文本；没有变化则省略 profile。\n"
            "Agent 核心人格由用户手动维护，你只能更新 agentSideNotes。\n"
            "notes 里可输出 add/update/archive 动作；update/archive 必须带已有 note id。\n\n"
            "当前记忆:\n"
            f"{json.dumps(compact_state, ensure_ascii=False)}\n\n"
            "新原始事件:\n"
            f"{json.dumps(event, ensure_ascii=False)}\n\n"
            "仅返回 JSON:\n"
            '{ "profile": "可选，完整用户画像", '
            '"agentSideNotes": "可选，完整 Agent 自动旁注", '
            '"notes": ['
            '{"action":"add|update|archive","id":"可选","text":"笔记内容","tags":["标签"],"confidence":0.0,"reason":"归档原因"}'
            "] }"
        )

    @staticmethod
    async def _distill_with_model(agent_config: AgentConfig, prompt: str) -> str:
        if agent_config.provider == "anthropic":
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=agent_config.api_key,
                base_url=agent_config.base_url or None,
            )
            response = await client.messages.create(
                model=agent_config.model_id or "claude-sonnet-4-20250514",
                max_tokens=2048,
                system="你只输出合法 JSON，不输出 Markdown。",
                messages=[{"role": "user", "content": prompt}],
            )
            text_parts = [block.text for block in response.content if block.type == "text"]
            return "".join(text_parts)

        from agno.agent import Agent
        from agno.models.openai import OpenAIResponses
        from agno.models.openai.like import OpenAILike

        if agent_config.provider == "openai-like":
            if not agent_config.base_url:
                raise ValueError("openai-like 模式需要配置 Base URL。")
            model = OpenAILike(
                id=agent_config.model_id or "gpt-5-mini",
                api_key=agent_config.api_key,
                base_url=agent_config.base_url,
            )
        else:
            model = OpenAIResponses(
                id=agent_config.model_id or "gpt-5-mini",
                api_key=agent_config.api_key,
            )

        agent = Agent(model=model, markdown=False, instructions=["你只输出合法 JSON。"])
        result = await agent.arun(prompt)
        content = getattr(result, "content", None)
        return content if isinstance(content, str) else str(result)
