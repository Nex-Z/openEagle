const http = require("node:http");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const devUrl = "http://127.0.0.1:1420";
const isWindows = process.platform === "win32";

function pnpmCliPath() {
  const candidates = [
    process.env.npm_execpath,
    isWindows && process.env.APPDATA
      ? path.join(process.env.APPDATA, "npm", "node_modules", "pnpm", "bin", "pnpm.cjs")
      : "",
  ].filter(Boolean);
  return candidates.find((candidate) => candidate && candidate.includes("pnpm") && require("node:fs").existsSync(candidate));
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

function runChecked(command, args) {
  const result = spawnSync(command, args, {
    cwd: root,
    env: process.env,
    shell: false,
    stdio: "inherit",
  });
  if (result.error) {
    console.error(`[electron:dev] Failed to start ${command}: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function runPnpmChecked(args) {
  const resolved = pnpmCommand(args);
  const result = spawnSync(resolved.command, resolved.args, {
    cwd: root,
    env: process.env,
    shell: resolved.shell,
    stdio: "inherit",
  });
  if (result.error) {
    console.error(`[electron:dev] Failed to start pnpm: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

function urlReady(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve((res.statusCode ?? 500) < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
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

function spawnInherit(command, args) {
  return spawn(command, args, {
    cwd: root,
    env: { ...process.env, FORCE_COLOR: "1" },
    shell: false,
    stdio: "inherit",
  });
}

function spawnPnpmInherit(args) {
  const resolved = pnpmCommand(args);
  const child = spawn(resolved.command, resolved.args, {
    cwd: root,
    env: { ...process.env, FORCE_COLOR: "1" },
    shell: resolved.shell,
    stdio: "inherit",
  });
  child.on("error", (error) => {
    console.error(`[electron:dev] Failed to start pnpm: ${error.message}`);
  });
  return child;
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

async function main() {
  runPnpmChecked(["exec", "tsc", "-p", "tsconfig.electron.json"]);
  runChecked(process.execPath, [path.join(root, "scripts", "ensure-cjs-package.cjs")]);

  let vite = null;
  if (!(await urlReady(devUrl))) {
    vite = spawnPnpmInherit([
      "exec",
      "vite",
      "--host",
      "127.0.0.1",
      "--port",
      "1420",
      "--strictPort",
    ]);

    vite.on("exit", (code) => {
      if (code !== null && code !== 0) {
        console.error(`[electron:dev] Vite exited with code ${code}`);
      }
    });

    if (!(await waitForUrl(devUrl, 20_000))) {
      stop(vite);
      console.error("[electron:dev] Vite did not become ready on http://127.0.0.1:1420");
      process.exit(1);
    }
  }

  const electron = spawnPnpmInherit(["exec", "electron", "."]);
  const cleanup = () => stop(vite);
  process.on("SIGINT", () => {
    cleanup();
    electron.kill("SIGINT");
  });
  process.on("SIGTERM", () => {
    cleanup();
    electron.kill("SIGTERM");
  });
  electron.on("exit", (code) => {
    cleanup();
    process.exit(code ?? 0);
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
