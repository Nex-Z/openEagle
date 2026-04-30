<p align="center">
  <h1 align="center">openEagle</h1>
  <p align="center">A desktop AI agent that sees your screen and acts on your behalf.</p>
</p>

<p align="center">
  <a href="./README.zh-CN.md">中文文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-blue" alt="version" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license" />
  <img src="https://img.shields.io/badge/platform-Windows%20first%20%7C%20macOS%2FLinux%20planned-lightgrey" alt="platform" />
</p>

---

> openEagle is still early. Rough edges are expected, and issues are very welcome.

![openEagle demo](docs/demo.gif)

![openEagle screenshot](docs/image.png)

## Why openEagle

Most AI assistants can chat and run commands. Very few can **look at your screen and actually do the work**.

openEagle bridges the gap between "understanding what you want" and "actually doing it on your desktop." It watches your screen in real time, reasons about what it sees, and operates your mouse, keyboard, and applications — all while keeping you in control with a three-tier safety model.

**Chat mode** for when you need an AI that can search, read files, and run commands. **SOLO mode** for when you need an AI that can navigate GUIs, fill forms, and complete multi-step workflows across applications — the kind of tasks that normally require a human.

> Tried so far:
>  - Using a browser directly to collect information
>  - Controlling desktop apps, such as play/pause in a music client
>  - ...

## Quick Start (5 minutes)

### Prerequisites

- [Node.js](https://nodejs.org/) (with corepack)
- [Python](https://python.org/) >= 3.12
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Rust](https://rustup.rs/) (for Tauri)

### Install & Run

```bash
# Enable pnpm
corepack enable
corepack prepare pnpm@10.7.0 --activate

# Install frontend deps
pnpm install

# Install backend deps
uv sync --project ./backend

# Launch the app
pnpm tauri:dev
```

That's it. In development, Tauri starts the Python backend for you; packaged builds use the Python sidecar. Either way, you do not need to start the server manually.

### What's happening under the hood

```
Tauri (Rust)  →  starts uv/Python in dev, or the Python sidecar in packaged builds (random port)
Python        →  prints [AGENT_READY] WS_PORT: <port> to stdout
Rust          →  parses the port, notifies the frontend
Frontend      →  connects via WebSocket to ws://127.0.0.1:<port>/ws
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Tauri Shell (Rust)                  │
│  Process lifecycle · Screenshots · Input injection   │
│  Overlay window · Notifications                     │
├──────────────────────────────────────────────────────┤
│                Python Backend (FastAPI)               │
│  Agent loop · SOLO orchestration · Tool execution    │
│  Safety assessment · Prompt engine · Model routing   │
├──────────────────────────────────────────────────────┤
│               React Frontend (TypeScript)             │
│  Chat UI · SOLO overlay · Activity inspector         │
│  Settings · Dark/light theme · Responsive layout     │
└──────────────────────────────────────────────────────┘
         ↕ WebSocket (Envelope protocol)
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Desktop shell | Tauri 2, Rust |
| Frontend | React 18, TypeScript, Vite |
| Backend | Python 3.12+, FastAPI, WebSocket |
| LLM | OpenAI-compatible API (configurable) |
| Vision | VL model via OpenAI-compatible endpoint |
| Automation | mss (screenshots), pyautogui (input) |
| Agent framework | agno |
| Search | baidusearch (free, no API key) |
| Remote IM | Feishu long connection, Telegram Bot polling |

### SOLO Mode — How It Works

SOLO is a **goal-driven agent**, not a task executor with a fixed plan:

1. **Observe** — captures a screenshot of your desktop
2. **Think** — VL model analyzes the image + task goal + history
3. **Act** — executes one action (click, type, scroll, etc.)
4. **Repeat** — loops until the task is complete or you stop it

The model autonomously decides what to do at each step. No pre-programmed scripts. No brittle selectors. Just visual understanding and reasoning.

### Remote IM Control

openEagle can accept tasks from Feishu or Telegram after you enable the provider in **Settings -> IM**.

- Feishu requires the app `App ID` and `App Secret`. The whitelist accepts either `open_id` for a single user or `chat_id` for a private chat/group chat. Send one message first, then copy the blocked `open_id` / `chat_id` from the IM status panel if you are not sure what to fill in.
- Telegram requires a Bot Token. The whitelist accepts either `user_id` or `chat_id`.
- Empty whitelists reject all remote messages by default.
- Plain remote text starts a SOLO task by default. Use `/chat <message>` when you only want a text chat.

Remote commands:

| Command | Behavior |
|---------|----------|
| `<plain text>` | Start a SOLO desktop task |
| `/solo <task>` | Start a SOLO desktop task explicitly |
| `/chat <message>` | Send a Chat-only message |
| `/pause`, `/resume`, `/stop` | Control the current SOLO task |
| `/allow`, `/reject` | Approve or reject pending confirmations |
| `/help` | Show command help |

### Safety Model

Every action is evaluated against a three-tier risk model:

| Level | Behavior |
|-------|----------|
| `safe` | Executes immediately (low-risk desktop actions, read-only tools, etc.) |
| `confirm` | Waits for your approval (file writes, delete/move actions, system shortcuts, etc.) |
| `blocked` | Refuses outright (dangerous commands like `rm -rf`) |

Additional guardrails: max 150 steps, consecutive duplicate detection, screenshot no-change detection.

## Configuration

openEagle supports flexible model routing — separate providers for chat (text) and vision-language tasks, with configurable base URLs for any OpenAI-compatible API.

Key settings (accessible from the in-app Settings panel):

| Setting | Description |
|---------|-------------|
| `agent.provider` | Text model provider (`openai`, `openai-like`, `mock`) |
| `agent.modelId` | Text model for chat |
| `agent.baseUrl` | Custom OpenAI-compatible API base URL |
| `agent.vlProvider` | Vision model provider (`openai`, `openai-like`) |
| `agent.vlModelId` | Vision-Language model for SOLO |
| `agent.vlBaseUrl` | OpenAI-compatible API base URL for the vision model |
| `feishu.enabled` | Enable the Feishu remote entry |
| `feishu.appId` / `feishu.appSecret` | Feishu app credentials for long-connection events |
| `feishu.allowedOpenIds` / `feishu.allowedChatIds` | Feishu user/chat whitelist |
| `telegram.enabled` | Enable the Telegram remote entry |
| `telegram.botToken` | Telegram Bot API token |
| `telegram.allowedUserIds` / `telegram.allowedChatIds` | Telegram user/chat whitelist |
| `tools` | Custom tool definitions |
| `mcp` | MCP server connections |
| `skills` | Custom skill prompts |

## Roadmap

- [ ] Clearer execution status when you leave the main window
- [ ] Better support for more models
- [ ] Scheduled tasks, similar to OpenClaw
- [ ] Faster responses and actions
- [ ] Better self-iterating context management
- [ ] macOS and Linux support
- [ ] Community plugin system
- [ ] Multi-monitor awareness
- [ ] Session replay and richer history
- [ ] Voice input

## Contributing

Contributions are welcome! Here's how to get started:

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR_USERNAME/openEagle.git`
3. **Create** a branch: `git checkout -b feature/my-feature`
4. **Make** your changes
5. **Verify** before committing:
   ```bash
   pnpm -s tsc --noEmit          # Frontend type check
   uv run python -m compileall backend/app  # Backend syntax check
   cd src-tauri && cargo check   # Rust check (if modified)
   ```
6. **Commit** with a clear message following [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat: add multi-monitor support
   fix: resolve overlay position on ultrawide displays
   ```
7. **Push** and open a Pull Request

### Areas Where Help Is Needed

- **SOLO reliability** — testing across different applications and workflows
- **Context management** — stronger context strategies inspired by tools like OpenClaw and Hermes
- **Cross-platform** — macOS and Linux adaptation
- **Tools & integrations** — new built-in tools, MCP servers, skills
- **UI motion** — better visual design and interaction polish
- **Documentation** — guides, tutorials, translations
- **Tests** — backend unit tests, frontend component tests

See [AGENT.md](./AGENT.md) for detailed coding conventions.

## License

[MIT](./LICENSE)

---

<p align="center">
  Built with curiosity and too many screenshots.
</p>
