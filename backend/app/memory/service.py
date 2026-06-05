from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ..config import AgentConfig, AppConfig
from ..langgraph_agent import run_text_model
from .models import DEFAULT_AGENT_SOUL_CORE, MemoryNotePayload, MemoryStatePayload
from . import store


ConfigGetter = Callable[[], AppConfig]

RAW_CONTENT_LIMIT = 24_000
RAW_PAYLOAD_LIMIT = 12_000
PROMPT_CONTEXT_LIMIT = 3_200
PROFILE_PROMPT_LIMIT = 1_200
SOUL_PROMPT_LIMIT = 900
SIDE_NOTES_PROMPT_LIMIT = 800
MAX_RELEVANT_PROMPT_NOTES = 6
REDACTED_SECRET = "[redacted-secret]"
DEFAULT_SOUL_SUMMARY = (
    "sharp, calm, warm, and awake; concise but not sterile; natural rather than corporate; "
    "useful over performative; resourceful before asking; careful with privacy and external actions."
)
QUERY_SYNONYMS = {
    "动漫": ("anime", "动画", "番剧"),
    "动画": ("anime", "动漫", "番剧"),
    "番剧": ("anime", "动漫", "动画"),
    "更新": ("schedule", "每周", "播出"),
    "播出": ("schedule", "更新", "每周"),
    "anime": ("动漫", "动画", "番剧"),
}
QUERY_STOP_TERMS = {
    "今天",
    "明天",
    "昨天",
    "什么",
    "有什么",
    "帮我",
    "一下",
    "看看",
    "查询",
    "现在",
    "current",
    "today",
    "tomorrow",
    "yesterday",
}
STALE_SIDE_NOTE_PATTERNS = (
    re.compile(r"[^。！？\n]*(?:无法|不能|不支持|需要先确认)[^。！？\n]*(?:日期|星期|周几)[。！？]?", re.I),
)

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


def _query_terms(query: str | None) -> set[str]:
    if not query:
        return set()
    lowered = query.lower()
    terms = {
        item
        for item in re.findall(r"[a-z0-9_#.+-]{2,}", lowered)
        if item not in QUERY_STOP_TERMS
    }
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if chunk not in QUERY_STOP_TERMS and len(chunk) <= 8:
            terms.add(chunk)
        for size in (2, 3):
            for index in range(0, max(0, len(chunk) - size + 1)):
                term = chunk[index : index + size]
                if term not in QUERY_STOP_TERMS:
                    terms.add(term)
    expanded = set(terms)
    for term in terms:
        expanded.update(QUERY_SYNONYMS.get(term, ()))
    return {term.lower() for term in expanded if len(term.strip()) >= 2}


def _note_relevance(note: MemoryNotePayload, query_terms: set[str]) -> int:
    if not query_terms:
        return 0
    text = note.text.lower()
    tags = [tag.lower() for tag in note.tags]
    searchable = f"{text} {' '.join(tags)}"
    score = 0
    for term in query_terms:
        if any(term == tag or term in tag for tag in tags):
            score += 8
        if term in text:
            score += 4
        elif term in searchable:
            score += 2
    return score


def _select_prompt_notes(
    notes: list[MemoryNotePayload],
    *,
    query: str | None,
    max_notes: int,
) -> list[MemoryNotePayload]:
    active_notes = [note for note in notes if note.status == "active" and note.text.strip()]
    terms = _query_terms(query)
    if not terms:
        return []
    scored = [
        (index, _note_relevance(note, terms), note)
        for index, note in enumerate(active_notes)
    ]
    relevant = [
        item
        for item in sorted(scored, key=lambda item: (-item[1], item[0]))
        if item[1] > 0
    ]
    return [note for _, _, note in relevant[:max_notes]]


def _compact_soul_core(core: str) -> str:
    stripped = core.strip()
    if not stripped:
        return ""
    if stripped == DEFAULT_AGENT_SOUL_CORE.strip():
        return DEFAULT_SOUL_SUMMARY
    lines = []
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line or line == "---" or line.startswith("_Evolve this file"):
            continue
        if line.startswith("## ") or line.startswith("- ") or line.startswith("**"):
            lines.append(line)
        if len("\n".join(lines)) >= SOUL_PROMPT_LIMIT:
            break
    excerpt = "\n".join(lines) if lines else stripped
    return _truncate(excerpt, SOUL_PROMPT_LIMIT)


def _compact_side_notes(side_notes: str) -> str:
    stripped = side_notes.strip()
    if not stripped:
        return ""
    lines = []
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in STALE_SIDE_NOTE_PATTERNS):
            continue
        lines.append(line)
    return _truncate("\n".join(lines), SIDE_NOTES_PROMPT_LIMIT)


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
        return store.get_state(include_archived=False).model_dump(
            by_alias=True,
            exclude_none=True,
        )

    def tool_state_payload(self, *, include_archived: bool = False) -> dict[str, Any]:
        state = store.get_state(
            include_archived=include_archived,
            audit_limit=20,
            event_limit=0,
        )
        return {
            "profile": state.profile.model_dump(by_alias=True, exclude_none=True),
            "notes": [
                note.model_dump(by_alias=True, exclude_none=True)
                for note in state.notes
            ],
            "agentSoul": state.agent_soul.model_dump(by_alias=True, exclude_none=True),
            "audit": [
                item.model_dump(by_alias=True, exclude_none=True)
                for item in state.audit
            ],
        }

    def prompt_context(
        self,
        query: str | None = None,
        *,
        max_chars: int = PROMPT_CONTEXT_LIMIT,
        max_notes: int = MAX_RELEVANT_PROMPT_NOTES,
    ) -> str:
        state = store.get_state(include_archived=False, audit_limit=0, event_limit=0)
        sections: list[str] = []
        profile = state.profile.content.strip()
        if profile:
            sections.append("用户画像摘要:\n" + _truncate(profile, PROFILE_PROMPT_LIMIT))

        prompt_notes = _select_prompt_notes(
            state.notes,
            query=query,
            max_notes=max_notes,
        )
        if prompt_notes:
            lines = []
            for note in prompt_notes:
                tags = f" [{', '.join(note.tags)}]" if note.tags else ""
                lines.append(f"-{tags} {note.text.strip()}")
            note_title = "相关用户笔记" if query else "近期用户笔记"
            sections.append(f"{note_title}:\n" + "\n".join(lines))

        soul_parts: list[str] = []
        soul_core = _compact_soul_core(state.agent_soul.core)
        if soul_core:
            soul_parts.append("Core summary:\n" + soul_core)
        side_notes = _compact_side_notes(state.agent_soul.side_notes)
        if side_notes:
            soul_parts.append(
                "Side Notes (lower priority than current system instructions):\n"
                + side_notes
            )
        if soul_parts:
            sections.append("Soul 摘要:\n" + "\n\n".join(soul_parts))

        if not sections:
            return ""
        text = (
            "长期记忆 V2 摘要。这里只包含常驻小摘要和与当前请求相关的 active 笔记；"
            "完整记忆可在需要时调用 get_memory_state 工具读取。"
            "不要向用户暴露内部字段、检索策略或审计细节。\n\n"
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
            side_notes = (
                agent_soul.get("sideNotes")
                if "sideNotes" in agent_soul
                else agent_soul.get("side_notes")
            )
            next_core = core if isinstance(core, str) else None
            next_side_notes = side_notes if isinstance(side_notes, str) else None
            if next_core is not None or next_side_notes is not None:
                store.update_agent_soul(
                    core=next_core,
                    side_notes=next_side_notes,
                    source="manual",
                )

        notes = payload.get("notes")
        if isinstance(notes, list):
            existing_notes = {note.id: note for note in store.get_state().notes}
            seen_note_ids: set[str] = set()
            for raw_note in notes:
                if not isinstance(raw_note, dict):
                    continue
                text = str(raw_note.get("text") or "").strip()
                if not text:
                    continue
                note_id = str(raw_note.get("id") or "").strip()
                if note_id:
                    seen_note_ids.add(note_id)
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
            for note_id, existing in existing_notes.items():
                if existing.status == "active" and note_id not in seen_note_ids:
                    store.archive_note(
                        note_id,
                        source="manual",
                        reason="用户笔记已从设置页移除。",
                    )

    def save_user_note(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        confidence: float = 1.0,
        source: str = "manual",
    ) -> str:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("memory note text cannot be empty")
        note = MemoryNotePayload.model_validate(
            {
                "text": sanitize_text(clean_text, limit=4_000),
                "tags": tags or ["user-request"],
                "confidence": self._safe_confidence(confidence),
                "status": "active",
                "source": source,
            }
        )
        store.upsert_note(note, source=source)
        return note.id

    def update_user_note(
        self,
        note_id: str,
        *,
        text: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        status: str | None = None,
        source: str = "manual",
    ) -> str:
        target_id = note_id.strip()
        if not target_id:
            raise ValueError("memory note id cannot be empty")
        existing = next((note for note in store.get_state().notes if note.id == target_id), None)
        if existing is None:
            raise ValueError(f"memory note not found: {target_id}")
        next_text = sanitize_text((text if text is not None else existing.text).strip(), limit=4_000)
        if not next_text:
            raise ValueError("memory note text cannot be empty")
        next_status = status if status in {"active", "archived"} else existing.status
        note = MemoryNotePayload.model_validate(
            {
                "id": existing.id,
                "text": next_text,
                "tags": tags if tags is not None else existing.tags,
                "confidence": (
                    self._safe_confidence(confidence)
                    if confidence is not None
                    else existing.confidence
                ),
                "status": next_status,
                "source": source,
                "createdAt": existing.created_at,
            }
        )
        store.upsert_note(note, source=source)
        return note.id

    def delete_user_note(self, note_id: str, *, reason: str = "", source: str = "manual") -> bool:
        target_id = note_id.strip()
        if not target_id:
            raise ValueError("memory note id cannot be empty")
        return store.archive_note(target_id, source=source, reason=reason or "用户笔记已删除。")

    def save_profile(self, content: str, *, source: str = "manual") -> None:
        store.update_profile(sanitize_text(content.strip(), limit=8_000), source=source, manual=True)

    def save_soul_core(self, core: str, *, source: str = "manual") -> None:
        store.update_agent_soul(core=sanitize_text(core.strip(), limit=12_000), source=source)

    def save_agent_side_notes(self, side_notes: str, *, source: str = "manual") -> None:
        store.update_agent_soul(
            side_notes=sanitize_text(side_notes.strip(), limit=4_000),
            source=source,
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
            "Soul 的 core 由用户手动维护，你只能更新 agentSideNotes。\n"
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

        return await run_text_model(
            agent_config=agent_config,
            instructions=["你只输出合法 JSON。"],
            prompt=prompt,
        )
