from __future__ import annotations

import argparse
import asyncio
import json
import socket
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent_service import build_agent_service
from .config import AppConfig, load_config
from .models import Envelope, ErrorPayload, MessagePayload, StatusPayload, utc_now
from .runtime_state import RuntimeState

app = FastAPI(title="openEagle Agent Backend")
config = load_config()
runtime_state = RuntimeState()
runtime_state.update_config(config)


@app.on_event("startup")
async def announce_ready() -> None:
    port = getattr(app.state, "ws_port", None)
    if port is not None:
        print(f"[AGENT_READY] WS_PORT: {port}", flush=True)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def send_envelope(
    websocket: WebSocket,
    type_: str,
    request_id: str,
    conversation_id: str,
    payload: dict[str, Any],
) -> None:
    await websocket.send_text(
        json.dumps(
            {
                "type": type_,
                "requestId": request_id,
                "conversationId": conversation_id,
                "payload": payload,
                "timestamp": utc_now(),
            },
            ensure_ascii=False,
        )
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            envelope = Envelope.model_validate_json(raw)

            if envelope.type == "client:update_settings":
                next_config = AppConfig.model_validate(envelope.payload["settings"])
                runtime_state.update_config(next_config)
                await send_envelope(
                    websocket,
                    "server:status",
                    envelope.request_id,
                    envelope.conversation_id,
                    StatusPayload(stage="idle", detail="模型配置已同步").model_dump(),
                )
                continue

            if envelope.type != "client:send_message":
                await send_envelope(
                    websocket,
                    "server:error",
                    envelope.request_id,
                    envelope.conversation_id,
                    ErrorPayload(
                        message="Unsupported message type",
                        code="unsupported_type",
                    ).model_dump(),
                )
                continue

            payload = MessagePayload.model_validate(envelope.payload)

            await send_envelope(
                websocket,
                "server:status",
                envelope.request_id,
                envelope.conversation_id,
                StatusPayload(
                    stage="thinking",
                    detail="后端正在生成回复",
                ).model_dump(),
            )

            agent_service = build_agent_service(runtime_state.get_config())
            chunks: list[str] = []
            async for chunk in agent_service.stream_reply(
                envelope.conversation_id,
                payload.content,
            ):
                chunks.append(chunk)
                await send_envelope(
                    websocket,
                    "server:message_delta",
                    envelope.request_id,
                    envelope.conversation_id,
                    {"content": chunk},
                )

            reply = "".join(chunks)

            await send_envelope(
                websocket,
                "server:message",
                envelope.request_id,
                envelope.conversation_id,
                {"content": reply},
            )

            await send_envelope(
                websocket,
                "server:status",
                envelope.request_id,
                envelope.conversation_id,
                StatusPayload(stage="idle", detail="回复完成").model_dump(),
            )
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        await send_envelope(
            websocket,
            "server:error",
            "server-error",
            "unknown",
            ErrorPayload(message=str(exc), code="internal_error").model_dump(),
        )


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


async def serve(host: str, port: int) -> None:
    actual_port = port if port != 0 else find_free_port(host)
    app.state.ws_port = actual_port
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=actual_port,
            log_level="info",
            ws="websockets",
        )
    )
    await server.serve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()
