# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

openEagle is a desktop AI assistant — **Electron 42 + React frontend + Python FastAPI backend**. Users talk to one main agent:

- **Main agent**: direct conversation, intent understanding, clarification, routing, and summaries.
- **Workers**: scoped sub-agents for tools, coding, research, and visual desktop execution.
- **Desktop execution worker**: internally still named `solo` in protocol/config/code; VL model analyzes screenshots and operates the desktop.

## Architecture

```
Electron (Node.js) → launches Python sidecar (random port)
Python → stdout: [AGENT_READY] WS_PORT: <port>
Electron → parses port, notifies frontend via IPC
Frontend → WebSocket to ws://127.0.0.1:<port>/ws
```

All messages use **Envelope** pattern: `type`, `requestId`, `conversationId`, `payload`, `timestamp`.

- Client→Server: `client:<action>` | Server→Client: `server:<event>`
- Frontend types: `src/types/protocol.ts` | Backend models: `backend/app/models.py`

## Build Commands

```powershell
# Full app (recommended for development)
pnpm electron:dev

# Frontend only
pnpm dev                    # Vite dev server on :1420
pnpm -s tsc --noEmit        # Type check
pnpm -s build               # Production build

# Python backend
uv sync --project .\backend                          # Install deps
uv run python -m compileall backend\app              # Syntax check
uv run python -m unittest discover -s backend\tests  # Run tests

# Electron main process
pnpm exec tsc -p tsconfig.electron.json --noEmit     # Type check

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
  components/solo/      Desktop execution overlay (internal solo naming)
  hooks/                useBackendConnection (core state + WS), useTheme
  lib/electron-bridge.ts  Electron IPC bridge (invoke/listen/convertFileSrc)
  lib/storage.ts        localStorage persistence with quota protection
  types/protocol.ts     All shared TypeScript types
  App.tsx               Root — state lives here, passed via props
  styles.css            Single global stylesheet (CSS custom props for themes)

electron/               Electron main process (TypeScript)
  main.ts               App entry, BrowserWindow, protocol handler
  preload.ts            contextBridge exposing secure IPC to renderer
  ipc-handlers.ts       All IPC handler registrations
  backend-manager.ts    Python sidecar lifecycle management
  screenshot.ts         Screenshot capture (nut-js)
  input.ts              Mouse/keyboard automation (nut-js)
  conversations.ts      Conversation file persistence
  overlay.ts            Solo overlay BrowserWindow management
  log.ts                App log file writing

backend/                Python FastAPI server
  app/main.py           FastAPI app, WebSocket handler, agent loop
  app/models.py         Pydantic message models
  app/config.py         AppConfig (agent, tools, solo, permissions)
  app/safety.py         3-level risk: safe/confirm/blocked
  app/prompts.py        All LLM prompts (centralized)
  app/default_tools.py  Main agent/default tool implementations (Toolkit class)
  app/solo_service.py   Desktop execution session state (internal solo naming)
  app/solo_executor.py  Desktop action execution
  app/solo_kernel.py    Desktop execution agent kernel
  app/providers/        LLM providers (LangGraph, Anthropic, mock, base)
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

When adding new Electron IPC commands, update all three:
1. `electron/ipc-handlers.ts` — Register the handler
2. `electron/preload.ts` — Add channel to allowed list
3. `src/lib/electron-bridge.ts` — (optional) Add typed helper

## Pre-commit

1. Frontend: `pnpm -s tsc --noEmit`
2. Electron: `pnpm exec tsc -p tsconfig.electron.json --noEmit`
3. Backend: `uv run python -m compileall backend\app`
