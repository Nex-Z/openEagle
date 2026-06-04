const http = require("node:http");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const isWindows = process.platform === "win32";
const backendHost = process.env.OPEN_EAGLE_BACKEND_HOST || "127.0.0.1";
const backendPort = process.env.OPEN_EAGLE_BACKEND_PORT || "8765";
const healthUrl = `http://${backendHost}:${backendPort}/health`;

function pnpmCliPath() {
  const candidates = [
    process.env.npm_execpath,
    isWindows && process.env.APPDATA
      ? path.join(process.env.APPDATA, "npm", "node_modules", "pnpm", "bin", "pnpm.cjs")
      : "",
  ].filter(Boolean);
  return candidates.find((candidate) => candidate && candidate.includes("pnpm"));
}

function pnpmCommand(args) {
  const cliPath = pnpmCliPath();
  if (cliPath) {
    return {
      command: process.execPath,
      args: [cliPath, ...args],
      shell: false,
    };
  }
  return {
    command: "pnpm",
    args,
    shell: isWindows,
  };
}

function spawnPnpm(args, extraEnv = {}) {
  const resolved = pnpmCommand(args);
  return spawn(resolved.command, resolved.args, {
    cwd: root,
    env: { ...process.env, FORCE_COLOR: "1", ...extraEnv },
    shell: resolved.shell,
    stdio: "inherit",
  });
}

function stop(child) {
  if (!child || child.killed) return;
  if (isWindows) {
    spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
      stdio: "ignore",
    });
    return;
  }
  child.kill("SIGTERM");
}

function urlReady(url, timeoutMs = 1000) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve((res.statusCode ?? 500) < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForUrl(url, timeoutMs) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await urlReady(url)) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function main() {
  const sharedEnv = {
    OPEN_EAGLE_BACKEND_HOST: backendHost,
    OPEN_EAGLE_BACKEND_PORT: backendPort,
  };
  const backendAlreadyReady = await urlReady(healthUrl, 300);
  let backend = null;
  if (backendAlreadyReady) {
    console.log(`[dev:stack] Reusing backend already ready on ${healthUrl}`);
  } else {
    backend = spawnPnpm(["--filter", "@open-eagle/backend", "dev"], sharedEnv);
    backend.on("error", (error) => {
      console.error(`[dev:stack] Failed to start backend: ${error.message}`);
    });
  }

  const desktop = spawnPnpm(["run", "dev:desktop:external-backend"], sharedEnv);

  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    stop(desktop);
    stop(backend);
  };

  if (backend) {
    void waitForUrl(healthUrl, 30_000).then((ready) => {
      if (ready || cleaned) return;
      console.error(`[dev:stack] Backend did not become ready on ${healthUrl}`);
      cleanup();
      process.exit(1);
    });
  }

  process.on("exit", cleanup);
  process.on("SIGINT", () => {
    cleanup();
    process.exit(0);
  });
  process.on("SIGTERM", () => {
    cleanup();
    process.exit(0);
  });

  backend?.on("exit", (code) => {
    if (cleaned) return;
    console.error(`[dev:stack] Backend exited with code ${code}`);
    cleanup();
    process.exit(code ?? 1);
  });
  desktop.on("exit", (code) => {
    cleanup();
    process.exit(code ?? 0);
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
