const fs = require("node:fs");
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

function walkFiles(dir, predicate) {
  if (!fs.existsSync(dir)) return [];
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(fullPath, predicate));
    } else if (predicate(fullPath)) {
      files.push(fullPath);
    }
  }
  return files;
}

function mtimeMs(file) {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return 0;
  }
}

function needsElectronCompile() {
  const electronRoot = path.join(root, "electron");
  const outputRoot = path.join(root, "dist-electron");
  const outputs = walkFiles(outputRoot, (file) => file.endsWith(".js"));
  if (outputs.length === 0 || !fs.existsSync(path.join(outputRoot, "main.js"))) {
    return true;
  }

  const inputs = [
    path.join(root, "tsconfig.electron.json"),
    ...walkFiles(electronRoot, (file) => file.endsWith(".ts")),
  ];
  const newestInput = Math.max(...inputs.map(mtimeMs));
  const oldestOutput = Math.min(...outputs.map(mtimeMs));
  return newestInput > oldestOutput;
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
  let vite = null;
  let viteReady = Promise.resolve(true);
  if (!(await urlReady(devUrl, 300))) {
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

    viteReady = waitForUrl(devUrl, 20_000);
  }

  if (needsElectronCompile()) {
    runPnpmChecked(["exec", "tsc", "-p", "tsconfig.electron.json"]);
  }
  runChecked(process.execPath, [path.join(root, "scripts", "ensure-cjs-package.cjs")]);

  if (!(await viteReady)) {
    stop(vite);
    console.error("[electron:dev] Vite did not become ready on http://127.0.0.1:1420");
    process.exit(1);
  }

  const electron = spawnPnpmInherit(["exec", "electron", "--no-sandbox", "--disable-gpu", "--disable-gpu-compositing", "."]);
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    stop(vite);
    stop(electron);
  };
  process.on("exit", cleanup);
  process.on("SIGINT", () => {
    cleanup();
    process.exit(0);
  });
  process.on("SIGTERM", () => {
    cleanup();
    process.exit(0);
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
