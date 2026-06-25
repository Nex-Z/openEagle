from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Iterator, Literal, Protocol

from langfuse import Langfuse, propagate_attributes
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

logger = logging.getLogger(__name__)

MAX_CAPTURE_CHARS = 20_000
_SAFE_SPAN_PREFIX = "open-eagle."
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_auth_token",
    "_bot_token",
    "_password",
    "_refresh_token",
    "_secret",
)
_DATA_URL_PATTERN = re.compile(
    r"data:[^;,\s]+;base64,[A-Za-z0-9+/=\r\n]+",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"\b(?:sk|pk)-lf-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"""(?ix)
        (["']?(?:api[_-]?key|authorization|password|secret|token)["']?
        \s*[:=]\s*["']?)
        ([^"',\s}]+)
        """,
    ),
)


class Observation(Protocol):
    def update(self, **kwargs: Any) -> Any: ...


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _release_version() -> str:
    try:
        return version("open-eagle-agent")
    except PackageNotFoundError:
        return "dev"


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_text(value: str) -> str:
    redacted = _DATA_URL_PATTERN.sub("[REDACTED BASE64 DATA]", value)
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.groups == 2:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED CREDENTIAL]", redacted)
    if len(redacted) <= MAX_CAPTURE_CHARS:
        return redacted
    omitted = len(redacted) - MAX_CAPTURE_CHARS
    return f"{redacted[:MAX_CAPTURE_CHARS]}\n...[truncated {omitted} chars]"


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[MAX DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return _redact_text(value)
            return _sanitize_value(parsed, depth=depth + 1)
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _sanitize_value(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, depth=depth + 1) for item in list(value)[:100]]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _sanitize_value(model_dump(), depth=depth + 1)
        except Exception:
            pass
    return _redact_text(str(value))


def trace_value(value: Any, *, capture_content: bool | None = None) -> Any:
    capture = _CAPTURE_CONTENT if capture_content is None else capture_content
    if capture:
        return _sanitize_value(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return {
            "contentCaptured": False,
            "type": "text",
            "characters": len(value),
        }
    if isinstance(value, dict):
        return {
            "contentCaptured": False,
            "type": "object",
            "fields": sorted(str(key) for key in value)[:30],
        }
    if isinstance(value, (list, tuple, set)):
        return {
            "contentCaptured": False,
            "type": "list",
            "items": len(value),
        }
    return {"contentCaptured": False, "type": type(value).__name__}


def _is_content_attribute(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in {
            "langfuse.observation.input",
            "langfuse.observation.output",
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.system_instructions",
            "gen_ai.tool.definitions",
        }
        or normalized.startswith(("gen_ai.prompt", "gen_ai.completion"))
        or any(
            marker in normalized
            for marker in ("input_messages", "output_messages", "system_instructions")
        )
    )


def _otel_attribute_value(
    value: Any,
) -> str | bool | int | float | list[str] | list[bool] | list[int] | list[float]:
    sanitized = _sanitize_value(value)
    if isinstance(sanitized, (str, bool, int, float)):
        return sanitized
    if isinstance(sanitized, list) and sanitized:
        scalar_type = type(sanitized[0])
        if scalar_type in {str, bool, int, float} and all(
            type(item) is scalar_type for item in sanitized
        ):
            return sanitized
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))


def _mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
    patches = {}
    for identifier, span in params.spans.items():
        delete_attributes: list[str] = []
        set_attributes: dict[
            str,
            str | bool | int | float | list[str] | list[bool] | list[int] | list[float],
        ] = {}
        safe_span = span.name.startswith(_SAFE_SPAN_PREFIX)
        for key, value in span.attributes.items():
            if not _CAPTURE_CONTENT and _is_content_attribute(key):
                if not (
                    safe_span
                    and key
                    in {"langfuse.observation.input", "langfuse.observation.output"}
                ):
                    delete_attributes.append(key)
                    continue
            masked = _otel_attribute_value(value)
            if masked != value:
                set_attributes[key] = masked
        if delete_attributes or set_attributes:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=tuple(delete_attributes),
                set_attributes=set_attributes,
            )
    if not patches:
        return None
    return MaskOtelSpansResult(span_patches=patches)


_CAPTURE_CONTENT = _env_flag("LANGFUSE_CAPTURE_CONTENT", False)
_TRACING_CONFIGURED = bool(
    os.getenv("LANGFUSE_PUBLIC_KEY")
    and os.getenv("LANGFUSE_SECRET_KEY")
    and _env_flag("LANGFUSE_TRACING_ENABLED", True)
)
_client: Langfuse | None = None

if _TRACING_CONFIGURED:
    try:
        os.environ["TRACELOOP_TRACE_CONTENT"] = (
            "true" if _CAPTURE_CONTENT else "false"
        )
        _client = Langfuse(mask_otel_spans=_mask_otel_spans)
        logger.info(
            "Langfuse tracing enabled (content capture: %s)",
            "enabled" if _CAPTURE_CONTENT else "disabled",
        )
    except Exception:
        _client = None
        logger.exception("Langfuse initialization failed; tracing is disabled")

if _client is not None:
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()
    except Exception:
        logger.exception(
            "Anthropic OpenTelemetry instrumentation failed; other tracing remains enabled"
        )

if _client is not None:
    from langfuse.openai import AsyncOpenAI
else:
    from openai import AsyncOpenAI


def tracing_enabled() -> bool:
    return _client is not None


def _session_id(conversation_id: str) -> str:
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:32]
    return f"open-eagle-{digest}"


def _metadata_values(metadata: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        sanitized = _sanitize_value(value)
        if not isinstance(sanitized, str):
            sanitized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
        result[str(key)] = sanitized[:200]
    return result


@contextmanager
def trace_agent_request(
    *,
    name: str,
    request_id: str,
    conversation_id: str,
    input: Any,
    source: str,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> Iterator[Observation | None]:
    if _client is None:
        yield None
        return

    trace_id = _client.create_trace_id(seed=f"{name}:{request_id}")
    root_metadata = {
        "requestId": request_id,
        "source": source,
        **(metadata or {}),
    }
    with _client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name=name,
        as_type="agent",
        input=trace_value(input),
        metadata=_sanitize_value(root_metadata),
    ) as observation:
        with propagate_attributes(
            session_id=_session_id(conversation_id),
            metadata=_metadata_values(root_metadata),
            version=_release_version(),
            tags=["open-eagle", source, *(tags or [])],
            trace_name=name,
        ):
            yield observation


@contextmanager
def trace_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    kind: str = "tool",
) -> Iterator[Observation | None]:
    if _client is None:
        yield None
        return
    with _client.start_as_current_observation(
        name=f"{_SAFE_SPAN_PREFIX}tool.{name}",
        as_type="tool",
        input=trace_value(arguments),
        metadata={"kind": kind},
    ) as observation:
        yield observation


def update_observation(
    observation: Observation | None,
    *,
    output: Any | None = None,
    metadata: dict[str, Any] | None = None,
    level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
    status_message: str | None = None,
) -> None:
    if observation is None:
        return
    payload: dict[str, Any] = {}
    if output is not None:
        payload["output"] = trace_value(output)
    if metadata:
        payload["metadata"] = _sanitize_value(metadata)
    if level:
        payload["level"] = level
    if status_message:
        payload["status_message"] = _redact_text(status_message)
    if payload:
        observation.update(**payload)


def update_current_observation(*, metadata: dict[str, Any]) -> None:
    if _client is None:
        return
    _client.update_current_span(metadata=_sanitize_value(metadata))


def openai_observation_kwargs(
    name: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _client is None:
        return {}
    return {
        "name": name,
        "metadata": _sanitize_value(metadata or {}),
    }


def shutdown_langfuse() -> None:
    if _client is not None:
        _client.shutdown()
