from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import httpx

from app.config import AppConfig, VoiceInputConfig
from app.voice_transcription import VoiceTranscriptionError, transcribe_audio


class FakeAsyncClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def audio_base64() -> str:
    return base64.b64encode(b"voice-test").decode("ascii")


class VoiceInputConfigTest(unittest.TestCase):
    def test_config_accepts_frontend_aliases_and_clamps_duration(self) -> None:
        config = AppConfig.model_validate(
            {
                "voiceInput": {
                    "enabled": True,
                    "apiKey": "sk-test",
                    "baseUrl": "https://voice.example/v1",
                    "modelId": "qwen3-asr-flash",
                    "maxDurationSeconds": 120,
                },
            }
        )

        self.assertTrue(config.voice_input.enabled)
        self.assertEqual(config.voice_input.max_duration_seconds, 120)
        with self.assertRaises(ValueError):
            AppConfig.model_validate({"voiceInput": {"maxDurationSeconds": 301}})


class VoiceTranscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_does_not_call_upstream(self) -> None:
        with patch("app.voice_transcription.httpx.AsyncClient") as client_factory:
            with self.assertRaisesRegex(VoiceTranscriptionError, "API Key") as raised:
                await transcribe_audio(
                    VoiceInputConfig(enabled=True),
                    audio_base64=audio_base64(),
                    mime_type="audio/webm",
                )

        self.assertEqual(raised.exception.code, "voice_missing_api_key")
        client_factory.assert_not_called()

    async def test_calls_qwen_compatible_endpoint_and_returns_text(self) -> None:
        request = httpx.Request("POST", "https://voice.example/v1/chat/completions")
        client = FakeAsyncClient(
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": "  转写完成  "}}]},
                request=request,
            )
        )
        config = VoiceInputConfig(
            enabled=True,
            api_key="sk-secret",
            base_url="https://voice.example/v1/",
        )

        with patch("app.voice_transcription.httpx.AsyncClient", return_value=client):
            text = await transcribe_audio(
                config,
                audio_base64=audio_base64(),
                mime_type="audio/webm;codecs=opus",
            )

        self.assertEqual(text, "转写完成")
        self.assertEqual(client.calls[0]["url"], "https://voice.example/v1/chat/completions")
        headers = client.calls[0]["headers"]
        self.assertEqual(headers, {"Authorization": "Bearer sk-secret"})
        body = client.calls[0]["json"]
        self.assertEqual(body["model"], "qwen3-asr-flash")
        self.assertTrue(body["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/webm"))

    async def test_upstream_credential_error_does_not_expose_key(self) -> None:
        request = httpx.Request("POST", "https://voice.example/v1/chat/completions")
        client = FakeAsyncClient(httpx.Response(401, json={"error": "bad key"}, request=request))
        config = VoiceInputConfig(enabled=True, api_key="sk-secret", base_url="https://voice.example/v1")

        with patch("app.voice_transcription.httpx.AsyncClient", return_value=client):
            with self.assertRaisesRegex(VoiceTranscriptionError, "无效") as raised:
                await transcribe_audio(
                    config,
                    audio_base64=audio_base64(),
                    mime_type="audio/webm",
                )

        self.assertEqual(raised.exception.code, "voice_invalid_credentials")
        self.assertNotIn("sk-secret", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
