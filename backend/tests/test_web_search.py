from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from app.config import AppConfig, WebSearchConfig
from app.default_tools import build_default_tools
from app.solo_executor import SoloExecutor


def _response(status_code: int, payload: dict[str, object]) -> httpx.Response:
    request = httpx.Request("POST", "https://api.tavily.com/search")
    return httpx.Response(status_code, json=payload, request=request)


class WebSearchConfigTest(unittest.TestCase):
    def test_app_config_accepts_frontend_aliases(self) -> None:
        config = AppConfig.model_validate(
            {
                "webSearch": {
                    "provider": "tavily",
                    "apiKey": "tvly-test",
                    "searchDepth": "advanced",
                    "maxResults": 8,
                }
            }
        )

        self.assertEqual(config.web_search.api_key, "tvly-test")
        self.assertEqual(config.web_search.search_depth, "advanced")
        self.assertEqual(config.web_search.max_results, 8)


class TavilyWebSearchTest(unittest.TestCase):
    def test_missing_api_key_returns_setup_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = build_default_tools(
                workspace_root=Path(tmp),
                web_search_config=WebSearchConfig(),
            )

            with (
                patch.dict(os.environ, {"TAVILY_API_KEY": ""}),
                patch("app.default_tools.httpx.post") as post,
            ):
                result = tools.web_search("openEagle")

        self.assertIn("设置 → 联网搜索", result)
        post.assert_not_called()

    def test_disabled_provider_is_not_exposed_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = build_default_tools(
                workspace_root=Path(tmp),
                web_search_config=WebSearchConfig(provider="disabled"),
            )

        self.assertNotIn("web_search", {tool.name for tool in tools.agent_tools})

    def test_search_calls_tavily_and_formats_results(self) -> None:
        payload = {
            "results": [
                {
                    "title": "openEagle",
                    "url": "https://example.com/open-eagle",
                    "content": "桌面 AI 助手。",
                    "published_date": "2026-06-20",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            tools = build_default_tools(
                workspace_root=Path(tmp),
                web_search_config=WebSearchConfig(
                    api_key="tvly-test",
                    search_depth="advanced",
                    max_results=7,
                ),
            )

            with patch(
                "app.default_tools.httpx.post",
                return_value=_response(200, payload),
            ) as post:
                result = tools.web_search(" openEagle ")

        self.assertIn("Tavily 搜索「openEagle」", result)
        self.assertIn("https://example.com/open-eagle", result)
        self.assertIn("发布时间：2026-06-20", result)
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["Authorization"], "Bearer tvly-test")
        self.assertEqual(request["json"]["search_depth"], "advanced")
        self.assertEqual(request["json"]["max_results"], 7)

    def test_result_count_is_clamped_to_tavily_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = build_default_tools(
                workspace_root=Path(tmp),
                web_search_config=WebSearchConfig(api_key="tvly-test"),
            )

            with patch(
                "app.default_tools.httpx.post",
                return_value=_response(200, {"results": []}),
            ) as post:
                tools.web_search("openEagle", max_results=99)

        self.assertEqual(post.call_args.kwargs["json"]["max_results"], 20)

    def test_invalid_api_key_has_safe_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = build_default_tools(
                workspace_root=Path(tmp),
                web_search_config=WebSearchConfig(api_key="tvly-bad"),
            )

            with patch(
                "app.default_tools.httpx.post",
                return_value=_response(401, {"detail": "Unauthorized"}),
            ):
                result = tools.web_search("openEagle")

        self.assertEqual(result, "搜索出错：Tavily API Key 无效或无权访问。")
        self.assertNotIn("tvly-bad", result)

    def test_solo_executor_preserves_configured_default_result_count(self) -> None:
        default_tools = Mock()
        default_tools.web_search.return_value = "ok"
        executor = object.__new__(SoloExecutor)
        executor._default_tools = default_tools

        result = executor.execute_action("web_search", {"query": "openEagle"})

        self.assertEqual(result["output"], "ok")
        default_tools.web_search.assert_called_once_with("openEagle", None)


if __name__ == "__main__":
    unittest.main()
