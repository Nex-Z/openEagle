# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

openEagle is a desktop AI assistant — **Tauri 2 (Rust) + React frontend + Python FastAPI backend**. Two modes:

- **Chat**: LLM conversation with tool calling (file ops, commands, code search, web search via Baidu)
- **SOLO**: Vision-Language desktop automation — VL model analyzes screenshots and operates the desktop

## Architecture

```
Tauri (Rust) → launches Python sidecar (random port)
Python → stdout: [AGENT_READY] WS_PORT: <port>
Rust → parses port, notifies frontend via Tauri event
Frontend → WebSocket to ws://127.0.0.1:<port>/ws
```

All messages use **Envelope** pattern: `type`, `requestId`, `conversationId`, `payload`, `timestamp`.

- Client→Server: `client:<action>` | Server→Client: `server:<event>`
- Frontend types: `src/types/protocol.ts` | Backend models: `backend/app/models.py`

## Build Commands

```powershell
# Full app (recommended for development)
pnpm tauri:dev

# Frontend only
pnpm dev                    # Vite dev server on :1420
pnpm -s tsc --noEmit        # Type check
pnpm -s build               # Production build

# Python backend
uv sync --project .\backend                          # Install deps
uv run python -m compileall backend\app              # Syntax check
uv run python -m unittest discover -s backend\tests  # Run tests

# Rust only
cd src-tauri && cargo check

# Sidecar packaging
.\backend\scripts\build-sidecar.ps1
```

## Key Directories

```
src/                    React frontend (TypeScript)
  components/chat/      ChatWorkspace (message list + input)
  components/inspector/ ActivityInspector, SoloPlanChecklist
  components/layout/    AppShell, NavigationSidebar
  components/settings/  SettingsDrawer
  components/solo/      SoloOverlay (separate window)
  hooks/                useBackendConnection (core state + WS), useTheme
  lib/storage.ts        localStorage persistence with quota protection
  types/protocol.ts     All shared TypeScript types
  App.tsx               Root — state lives here, passed via props
  styles.css            Single global stylesheet (CSS custom props for themes)

src-tauri/              Tauri Rust shell
  src/main.rs           Sidecar lifecycle, overlay window, screenshots, input injection
  capabilities/         Permission definitions (default.json)
  binaries/             Sidecar executables

backend/                Python FastAPI server
  app/main.py           FastAPI app, WebSocket handler, agent loop
  app/models.py         Pydantic message models
  app/config.py         AppConfig (agent, tools, solo, permissions)
  app/safety.py         3-level risk: safe/confirm/blocked
  app/prompts.py        All LLM prompts (centralized)
  app/default_tools.py  Chat tool implementations (Toolkit class)
  app/solo_service.py   SOLO session state
  app/solo_executor.py  SOLO action execution
  app/solo_kernel.py    SOLO agent kernel
  app/providers/        LLM providers (agno, mock, base)
```

## Conventions

**Read `AGENT.md` for detailed coding conventions.** Key points:

- UI text/comments/docs in **Chinese**, code identifiers in **English**
- TypeScript: strict mode, functional components + Hooks, state in `App.tsx` + `useBackendConnection`
- Styling: single `styles.css` with `data-theme` custom properties, no CSS modules
- Python: `from __future__ import annotations`, Pydantic v2, package manager is `uv` (never pip)
- All files UTF-8 without BOM
- Commit messages: `feat:` / `fix:` / `refactor:` + Chinese summary

## Protocol Extension

When adding new WebSocket message types, update all three:
1. `src/types/protocol.ts` — TypeScript type
2. `backend/app/models.py` — Pydantic model
3. `backend/app/main.py` — Handler/sender

## Pre-commit

1. Frontend: `pnpm -s tsc --noEmit`
2. Backend: `uv run python -m compileall backend\app`
3. Rust (if changed): `cd src-tauri && cargo check`
