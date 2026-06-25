from __future__ import annotations

import unittest
from unittest.mock import patch

from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

from app import observability


class ObservabilityTest(unittest.TestCase):
    def test_trace_value_hides_content_by_default(self) -> None:
        value = observability.trace_value("private conversation", capture_content=False)

        self.assertEqual(value["contentCaptured"], False)
        self.assertEqual(value["characters"], 20)
        self.assertNotIn("private conversation", str(value))

    def test_sanitizer_redacts_credentials_and_base64(self) -> None:
        value = observability._sanitize_value(
            {
                "apiKey": "sk-proj-super-secret-value",
                "max_tokens": 2048,
                "image": "data:image/png;base64,AAAAABBBBBCCCC",
            }
        )

        self.assertEqual(value["apiKey"], "[REDACTED]")
        self.assertEqual(value["max_tokens"], 2048)
        self.assertEqual(value["image"], "[REDACTED BASE64 DATA]")

    def test_session_id_is_stable_and_pseudonymous(self) -> None:
        first = observability._session_id("im_feishu_user_123")
        second = observability._session_id("im_feishu_user_123")

        self.assertEqual(first, second)
        self.assertNotIn("feishu_user_123", first)
        self.assertLess(len(first), 200)

    def test_mask_removes_provider_content_but_keeps_model_usage(self) -> None:
        identifier = OtelSpanIdentifier(trace_id="a" * 32, span_id="b" * 16)
        span = OtelSpanData(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            name="anthropic.chat",
            instrumentation_scope_name="opentelemetry.instrumentation.anthropic",
            instrumentation_scope_version="0.61.0",
            attributes={
                "gen_ai.input.messages": '[{"content":"private"}]',
                "gen_ai.output.messages": '[{"content":"secret"}]',
                "gen_ai.request.model": "claude-test",
                "gen_ai.usage.input_tokens": 42,
            },
            resource_attributes={},
        )

        with patch.object(observability, "_CAPTURE_CONTENT", False):
            result = observability._mask_otel_spans(
                params=MaskOtelSpansParams(spans={identifier: span})
            )

        self.assertIsNotNone(result)
        patch_value = result.span_patches[identifier]
        self.assertIn("gen_ai.input.messages", patch_value.delete_attributes)
        self.assertIn("gen_ai.output.messages", patch_value.delete_attributes)
        self.assertNotIn("gen_ai.request.model", patch_value.delete_attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", patch_value.delete_attributes)

    def test_mask_preserves_safe_open_eagle_summary(self) -> None:
        identifier = OtelSpanIdentifier(trace_id="c" * 32, span_id="d" * 16)
        span = OtelSpanData(
            trace_id="c" * 32,
            span_id="d" * 16,
            parent_span_id=None,
            name="open-eagle.chat-message",
            instrumentation_scope_name="langfuse-sdk",
            instrumentation_scope_version="4.9.1",
            attributes={
                "langfuse.observation.input": (
                    '{"contentCaptured":false,"type":"text","characters":12}'
                ),
            },
            resource_attributes={},
        )

        with patch.object(observability, "_CAPTURE_CONTENT", False):
            result = observability._mask_otel_spans(
                params=MaskOtelSpansParams(spans={identifier: span})
            )

        if result is not None:
            patch_value = result.span_patches[identifier]
            self.assertNotIn(
                "langfuse.observation.input",
                patch_value.delete_attributes,
            )


if __name__ == "__main__":
    unittest.main()
