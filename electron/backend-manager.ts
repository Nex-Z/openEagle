import { ChildProcess, spawn, execSync } from "node:child_process";
import path from "node:path";
import { BrowserWindow } from "electron";
import fs from "node:fs";

const READY_PATTERN = /\[AGENT_READY\]\s+WS_PORT:\s+(\d+)/;
const BACKEND_PARENT_PID_ENV = "OPEN_EAGLE_PARENT_PID";

export interface BackendState {
  phase: "starting" | "ready" | "error" | "disconnected";
  port: number | null;
  message: string;
}

type StateListener = (state: BackendState) => void;

let currentState: BackendState = {
  phase: "starting",
  port: null,
  message: "Desktop shell is booting the backend",
};

let childProcess: ChildProcess | null = null;
let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
const listeners: StateListener[] = [];

export function getBackendState(): BackendState {
  return { ...currentState };
}

export function onBackendStateChange(listener: StateListener): () => void {
  listeners.push(listener);
  return () => {
    const index = listeners.indexOf(listener);
    if (index >= 0) listeners.splice(index, 1);
  };
}

function setState(next: BackendState, mainWindow: BrowserWindow | null) {
  currentState = { ...next };
  for (const listener of listeners) {
    listener(currentState);
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("backend://status", currentState);
  }
}

function projectRoot(): string {
  return path.resolve(__dirname, "..");
}

function backendRoot(): string {
  return path.join(projectRoot(), "backend");
}

function backendPython(): string {
  return path.join(backendRoot(), ".venv", "Scripts", "python.exe");
}

function packagedSidecarPath(): string {
  const base = path.join(
    process.resourcesPath || projectRoot(),
    "backend",
    "binaries",
    "open-eagle-agent"
  );
  if (process.platform !== "win32") return base;
  const exe = `${base}.exe`;
  return fs.existsSync(exe) ? exe : base;
}

function cleanupStaleDebugBackends() {
  if (!process.env.NODE_ENV?.includes("dev") && !process.argv.includes("--dev")) return;
  try {
    const python = backendPython();
    const backend = backendRoot();
    execSync(
      `powershell -NoProfile -ExecutionPolicy Bypass -Command ` +
        `"Get-CimInstance Win32_Process -Filter \\\"Name = 'python.exe'\\\" | ` +
        `Where-Object { $_.CommandLine -like '*${python}*' -and $_.CommandLine -like '*-m app.main*' } | ` +
        `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"`,
      { stdio: "ignore", timeout: 5000 }
    );
  } catch {
    // best-effort cleanup
  }
}

export function spawnBackend(mainWindow: BrowserWindow | null, isDev: boolean) {
  setState(
    { phase: "starting", port: null, message: "Starting Python backend" },
    mainWindow
  );

  cleanupStaleDebugBackends();
  const parentPid = String(process.pid);

  let cmd: string;
  let args: string[];
  let cwd: string;

  if (isDev) {
    const python = backendPython();
    if (fs.existsSync(python)) {
      cmd = python;
      args = ["-m", "app.main", "--host", "127.0.0.1", "--port", "0"];
      cwd = backendRoot();
    } else {
      cmd = "uv";
      args = ["run", "python", "-m", "app.main", "--host", "127.0.0.1", "--port", "0"];
      cwd = backendRoot();
    }
  } else {
    const sidecarPath = packagedSidecarPath();
    cmd = sidecarPath;
    args = ["--host", "127.0.0.1", "--port", "0"];
    cwd = path.dirname(sidecarPath);
  }

  const env = {
    ...process.env,
    PYTHONUTF8: "1",
    PYTHONUNBUFFERED: "1",
    [BACKEND_PARENT_PID_ENV]: parentPid,
  };

  try {
    childProcess = spawn(cmd, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
  } catch (error) {
    setState(
      { phase: "error", port: null, message: `Backend failed to start: ${error}` },
      mainWindow
    );
    return;
  }

  childProcess.on("error", (error) => {
    setState(
      { phase: "error", port: null, message: `Backend process error: ${error.message}` },
      mainWindow
    );
  });

  childProcess.on("exit", (code) => {
    childProcess = null;
    const message =
      code === 0
        ? "Backend process exited"
        : `Backend process exited unexpectedly: code=${code}`;
    setState({ phase: "disconnected", port: null, message }, mainWindow);
  });

  // 12-second handshake timeout
  if (timeoutHandle) clearTimeout(timeoutHandle);
  timeoutHandle = setTimeout(() => {
    if (currentState.phase === "starting" && currentState.port === null) {
      setState(
        {
          phase: "error",
          port: null,
          message: "Backend handshake timed out before a port was reported",
        },
        mainWindow
      );
    }
  }, 12_000);

  const readyRegex = READY_PATTERN;

  childProcess.stdout?.on("data", (chunk: Buffer) => {
    const text = chunk.toString("utf-8").trimEnd();
    if (!text) return;
    console.log(`[BACKEND/stdout] ${text}`);
    appendLogFile(`[BACKEND/stdout] ${text}`);

    const match = readyRegex.exec(text);
    if (match) {
      const port = parseInt(match[1], 10);
      if (Number.isFinite(port)) {
        if (timeoutHandle) {
          clearTimeout(timeoutHandle);
          timeoutHandle = null;
        }
        setState({ phase: "ready", port, message: `Backend is ready on port ${port}` }, mainWindow);
      }
    }
  });

  childProcess.stderr?.on("data", (chunk: Buffer) => {
    const text = chunk.toString("utf-8").trimEnd();
    if (!text) return;
    console.error(`[BACKEND/stderr] ${text}`);
    appendLogFile(`[BACKEND/stderr] ${text}`);

    if (currentState.phase !== "ready") {
      setState(
        { phase: "starting", port: null, message: `Backend boot output: ${text}` },
        mainWindow
      );
    }
  });
}

export function killBackend() {
  if (timeoutHandle) {
    clearTimeout(timeoutHandle);
    timeoutHandle = null;
  }
  if (childProcess) {
    try {
      childProcess.kill();
    } catch {
      // process may already be dead
    }
    childProcess = null;
  }
}

// Simple log file appender (mirrors Rust append_app_log)
import { appendFileSync, mkdirSync } from "node:fs";

let logPath: string | null = null;

export function initLogPath() {
  const dir = path.join(projectRoot(), ".open-eagle", "logs");
  mkdirSync(dir, { recursive: true });
  const date = new Date().toISOString().slice(0, 10);
  logPath = path.join(dir, `openEagle-${date}.log`);
  console.log(`[APP] log file: ${logPath}`);
}

function appendLogFile(line: string) {
  if (!logPath) return;
  try {
    appendFileSync(logPath, `${new Date().toISOString()} ${line}\n`);
  } catch {
    // best-effort
  }
}
