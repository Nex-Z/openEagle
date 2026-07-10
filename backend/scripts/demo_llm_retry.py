"""演示 LLM 调用退避重试（基础版）。

本地起一个"前两次返回 429、第三次返回 200"的 HTTP 服务器，
用真实 openai client + 真实 retry_llm_call 跑通：
能看到 [BACKEND] llm retry 日志，并最终成功。

运行（在 backend 目录下）:
    uv run python scripts/demo_llm_retry.py
"""
from __future__ import annotations

import asyncio
import http.server
import sys
import threading

# Windows 控制台默认 GBK，打不出 emoji/部分中文，强制 UTF-8
sys.stdout.reconfigure(encoding="utf-8")

from openai import AsyncOpenAI

from app.llm_retry import retry_llm_call

# 第三次请求返回的合法 chat completion 响应体
_SUCCESS_BODY = (
    '{"id":"demo","object":"chat.completion","created":0,"model":"demo",'
    '"choices":[{"index":0,"message":{"role":"assistant","content":"重试后成功！"},'
    '"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
).encode("utf-8")


class _FlakyHandler(http.server.BaseHTTPRequestHandler):
    counter = 0

    def do_POST(self) -> None:  # noqa: N802
        _FlakyHandler.counter += 1
        if _FlakyHandler.counter < 3:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"rate limited"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_SUCCESS_BODY)

    def log_message(self, *args: object) -> None:  # 静默默认访问日志
        pass


def _start_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def main() -> None:
    server, port = _start_server()
    try:
        # max_retries=0 关掉 SDK 内置重试，只让我们的 wrapper 重试
        client = AsyncOpenAI(
            api_key="sk-fake",
            base_url=f"http://127.0.0.1:{port}",
            max_retries=0,
        )
        result = await retry_llm_call(
            lambda: client.chat.completions.create(
                model="demo",
                messages=[{"role": "user", "content": "hi"}],
            ),
            label="demo",
        )
        print(f"\n✅ 最终成功：{result.choices[0].message.content}")
        print(
            f"   服务器共收到 {_FlakyHandler.counter} 次请求"
            "（前 2 次 429，第 3 次 200）"
        )
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
