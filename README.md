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

openEagle bridges the gap between "understanding what you want" and "actually doing it on your desktop." You talk to one main agent. It can answer directly, use tools, delegate focused work, or dispatch a desktop execution worker that watches your screen and operates your mouse, keyboard, and applications — all while keeping you in control with a three-tier safety model.

There is no mode switch to choose. The main agent decides whether the right response is a natural answer, a tool-backed worker task, or visual desktop execution for GUI workflows such as navigating apps, filling forms, and completing multi-step operations.

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
│  Main/Sub-agent runtime · Desktop execution · Tools │
│  Safety assessment · Prompt engine · Model routing   │
├──────────────────────────────────────────────────────┤
│               React Frontend (TypeScript)             │
│  Conversation UI · Desktop overlay · Activity panel  │
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
| Remote IM | Feishu long connection, Telegram Bot polling, WeChat ClawBot QR binding |

### Main/Sub-Agent Runtime

Every user message first goes through a lightweight main agent. The main agent decides whether to answer directly, delegate to a clean worker, start the desktop execution worker, or control an existing desktop execution task. Workers use scoped internal conversation IDs, so task execution stays focused while the visible conversation only keeps summaries, evidence, and final results.

Built-in worker kinds:

| Worker | Use case |
|--------|----------|
| `general` | General explanation, light tool use, read-only checks |
| `coding` | Code edits, docs, builds, tests, file-changing work |
| `research` | Search, lookup, information gathering |
| `solo` | Internal worker id for visual desktop execution |

Read-only workers may run concurrently. Coding/write work and desktop execution are kept serial to avoid file and desktop conflicts.

### Desktop Execution — How It Works

Desktop execution is a **goal-driven worker**, not a task executor with a fixed plan:

1. **Observe** — captures a screenshot of your desktop
2. **Think** — VL model analyzes the image + task goal + history
3. **Act** — executes one action (click, type, scroll, etc.)
4. **Repeat** — loops until the task is complete or you stop it

The model autonomously decides what to do at each step. No pre-programmed scripts. No brittle selectors. Just visual understanding and reasoning.

The desktop execution worker uses soft stability signals instead of rigid scripts: action signatures detect true repeated actions, visual no-op and uncertain outcomes trigger recovery hints, and batch actions are only suppressed while recovering. Navigation-like actions such as `open_url`, click, double-click, and `press_keys` use short post-action screenshot stabilization so the next model step sees the loaded page instead of an early loading frame. This keeps the worker flexible for general tasks while still reducing repeated clicks and stalled screens.

### Remote IM Control

openEagle can accept tasks from Feishu, Telegram, or WeChat after you enable the provider in **Settings -> IM**.

- Feishu requires the app `App ID` and `App Secret`. The whitelist accepts either `open_id` for a single user or `chat_id` for a private chat/group chat. Send one message first, then copy the blocked `open_id` / `chat_id` from the IM status panel if you are not sure what to fill in.
- Telegram requires a Bot Token. The whitelist accepts either `user_id` or `chat_id`.
- WeChat uses `wechat-clawbot`. Click "Scan to bind" in the WeChat card, scan the QR code with WeChat, then enable the entry after the `accountId` is saved. "Unbind" stops polling and removes the local ClawBot account credentials used by openEagle.
- Empty whitelists reject all remote messages by default.
- Plain remote text is handled by the main agent first. It can reply naturally, use tools, or dispatch desktop execution when the task needs GUI control.
- Use `/solo <task>` only when you want to explicitly bias the request toward desktop execution.

Remote commands:

| Command | Behavior |
|---------|----------|
| `<plain text>` | Send the message to the main agent |
| `/solo <task>` | Explicitly request desktop execution |
| `/pause`, `/resume`, `/stop` | Control the current desktop execution task |
| `/allow`, `/reject` | Approve or reject pending confirmations |
| `/help` | Show command help |

### Safety Model

Every action is evaluated against a three-tier risk model:

| Level | Behavior |
|-------|----------|
| `safe` | Executes immediately (low-risk desktop actions, read-only tools, etc.) |
| `confirm` | Waits for your approval (file writes, delete/move actions, system shortcuts, etc.) |
| `blocked` | Refuses outright (dangerous commands like `rm -rf`) |

Additional guardrails: max 150 steps, action-signature duplicate detection, screenshot no-change detection, recovery-mode batch suppression, and feedback loops that return malformed actions or execution errors to the active agent for self-repair before surfacing them to the user.

## Configuration

openEagle supports flexible model routing — separate providers for main-agent text work and vision-language desktop execution, with configurable base URLs for any OpenAI-compatible API.

Key settings (accessible from the in-app Settings panel):

| Setting | Description |
|---------|-------------|
| `agent.provider` | Text model provider (`openai`, `openai-like`, `mock`) |
| `agent.modelId` | Text model for the main agent and direct conversation |
| `agent.baseUrl` | Custom OpenAI-compatible API base URL |
| `agent.vlProvider` | Vision model provider (`openai`, `openai-like`) |
| `agent.vlModelId` | Vision-Language model for desktop execution |
| `agent.vlBaseUrl` | OpenAI-compatible API base URL for the vision model |
| `feishu.enabled` | Enable the Feishu remote entry |
| `feishu.appId` / `feishu.appSecret` | Feishu app credentials for long-connection events |
| `feishu.allowedOpenIds` / `feishu.allowedChatIds` | Feishu user/chat whitelist |
| `telegram.enabled` | Enable the Telegram remote entry |
| `telegram.botToken` | Telegram Bot API token |
| `telegram.allowedUserIds` / `telegram.allowedChatIds` | Telegram user/chat whitelist |
| `wechat.enabled` | Enable the WeChat ClawBot remote entry |
| `wechat.accountId` | WeChat ClawBot account saved after QR binding |
| `wechat.baseUrl` / `wechat.botType` | Optional ClawBot API base URL and bot type |
| `wechat.allowedUserIds` / `wechat.allowedChatIds` | WeChat user/chat whitelist |
| `tools` | Custom command tools ([see below](#tool--custom-command-tools)) |
| `mcp` | MCP server connections ([see below](#mcp--connect-external-services)) |
| `skills` | Custom skill directives ([see below](#skill--inject-domain-specific-knowledge)) |

## Tools, MCP & Skills

openEagle's capabilities don't stop at the built-ins. Three extension mechanisms let you customize everything from what the agent *can do* to *how it does it*.

### Tool — Custom Command Tools

A Tool is the simplest extension: give the agent a shell command template, and it can call it during conversation or desktop execution.

Add tools in **Settings -> Tools**:

| Field | Description |
|-------|-------------|
| `name` | Tool name — the agent uses this and the description to decide when to invoke it |
| `command` | Shell command template with `{placeholder}` parameters the agent fills in |
| `description` | Tells the agent what this tool does and when to use it |
| `cwd` | Working directory (optional) |
| `timeout_ms` | Timeout, defaults to 30 seconds |

**Example**: A `run_tests` tool with command `uv run python -m pytest {test_path} -v` and description "Run pytest tests at the given path." The agent will pick it up automatically when it needs to run tests.

### MCP — Connect External Services

openEagle supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), so you can plug in any MCP server's tools. This lets you reuse the entire MCP ecosystem without writing integration code.

Configure in **Settings -> MCP** with three transport types:

| Transport | Description | Use case |
|-----------|-------------|----------|
| `stdio` | Launches a local process | Local MCP servers |
| `sse` | Server-Sent Events | Remote MCP servers |
| `http` | Streamable HTTP | Remote MCP servers |

Example (stdio): `id: "my-mcp"`, `name: "My MCP Server"`, `transport: "stdio"`, `endpoint: "npx -y @some/mcp-server"`

MCP tools are available in both desktop execution and chat. In default permission mode, MCP calls require user confirmation.

### Skill — Inject Domain-Specific Knowledge

Skill is openEagle's most distinctive extension. It doesn't execute code — it **injects a behavioral directive** into the agent: "When you encounter this kind of task, follow this approach instead of the generic one."

This solves a core problem: **general-purpose models take the well-known path, but your workflow has private knowledge**. For example:

- Your company's deploy process isn't `docker compose up` — it's a custom script chain
- You follow specific file organization, naming conventions, or commit styles
- The correct sequence for an operation differs from what a web search would suggest

Skills let you "teach" the agent your experience so it follows your way automatically.

Add skills in **Settings -> Skills**:

| Field | Description |
|-------|-------------|
| `name` | Skill name |
| `description` | One-line summary of when this skill applies |
| `prompt` | The full behavioral directive — the more specific, the better |

**Example**:

```
name: "Project Deploy"
description: "Correct procedure for deploying to the test environment"
prompt: |
  When deploying to the test environment, do NOT use docker compose.
  Correct steps:
  1. Run scripts/build.sh
  2. Run scripts/deploy-test.sh
  3. Check health: curl http://test.internal:8080/health
  4. If it fails, rollback: scripts/rollback.sh
```

**How it works**:

- **Auto-activated**: Enabled skills are injected into the system prompt during desktop execution. The agent matches them against the current task via the skill description.
- **Manual selection**: Type `/skill <name>` in chat to explicitly apply a skill for the current turn.

> Skills are **structured transfer of experience**. You don't write code — you just write down "the right way to do it," and the agent follows. This is far more reliable than letting the model guess or search for generic solutions.

### How They Fit Together

```
                ┌─────────────────────────────────┐
                │          Agent Decision          │
                │  "How should I do this task?"    │
                └──────────┬──────────────────────┘
                           │
            ┌──────────────┼──────────────────┐
            ▼              ▼                  ▼
     ┌──────────┐   ┌──────────┐      ┌──────────┐
     │   Tool   │   │   MCP    │      │  Skill   │
     │  Execute │   │ External │      │Behavioral│
     │ Command  │   │ Capabil. │      │Directive │
     │          │   │          │      │          │
     │  "What"  │   │  "With"  │      │   "How"  │
     └──────────┘   └──────────┘      └──────────┘
```

- **Tool** defines what commands the agent can run
- **MCP** defines what external capabilities are available
- **Skill** defines how the agent approaches specific scenarios

All three compose. For example: an MCP server that provides database queries, a Skill that encodes your team's query conventions, and a Tool that wraps a common analysis script — the agent will combine them automatically when the context fits.

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

- **Desktop execution reliability** — testing across different applications and workflows
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
