from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from .config import VoiceInputConfig


MAX_AUDIO_BASE64_CHARS = 10 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 75.0


@dataclass(frozen=True)
class VoiceTranscriptionError(Exception):
    message: str
    code: str


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _validate_audio(audio_base64: str, mime_type: str) -> None:
    if not mime_type.startswith("audio/"):
        raise VoiceTranscriptionError("录音格式无效，请重新录制。", "voice_invalid_audio")
    if len(audio_base64) > MAX_AUDIO_BASE64_CHARS:
        raise VoiceTranscriptionError("录音文件过大，请缩短后重试。", "voice_audio_too_large")
    try:
        base64.b64decode(audio_base64, validate=True)
    except ValueError as exc:
        raise VoiceTranscriptionError("录音数据无效，请重新录制。", "voice_invalid_audio") from exc


async def transcribe_audio(
    config: VoiceInputConfig,
    *,
    audio_base64: str,
    mime_type: str,
) -> str:
    if not config.enabled:
        raise VoiceTranscriptionError("请先在设置中启用语音输入。", "voice_not_enabled")
    api_key = (config.api_key or "").strip()
    if not api_key:
        raise VoiceTranscriptionError("请先在设置中填写语音输入 API Key。", "voice_missing_api_key")

    _validate_audio(audio_base64, mime_type)
    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    request_body = {
        "model": config.model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:{mime_type};base64,{audio_base64}",
                        },
                    },
                ],
            },
        ],
        "stream": False,
        "asr_options": {"enable_itn": False},
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_body,
            )
    except httpx.TimeoutException as exc:
        raise VoiceTranscriptionError("语音转写超时，请稍后重试。", "voice_timeout") from exc
    except httpx.HTTPError as exc:
        raise VoiceTranscriptionError("语音服务暂时不可用，请稍后重试。", "voice_network_error") from exc

    if response.status_code in {401, 403}:
        raise VoiceTranscriptionError("语音输入 API Key 无效或无权访问。", "voice_invalid_credentials")
    if response.is_error:
        raise VoiceTranscriptionError("语音转写失败，请检查模型与服务地址。", "voice_upstream_error")

    try:
        text = _response_text(response.json())
    except ValueError as exc:
        raise VoiceTranscriptionError("语音服务返回无效结果，请稍后重试。", "voice_invalid_response") from exc
    if not text:
        raise VoiceTranscriptionError("未识别到可用文字，请重新录制。", "voice_empty_result")
    return text
