const path = require("node:path");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const backendRoot = path.join(root, "backend");
const isWindows = process.platform === "win32";

function argValue(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index >= 0 && process.argv[index + 1]) {
    return process.argv[index + 1];
  }
  return fallback;
}

const host = argValue("--host", process.env.OPEN_EAGLE_BACKEND_HOST || "127.0.0.1");
const port = argValue("--port", process.env.OPEN_EAGLE_BACKEND_PORT || "8765");

const child = spawn(
  "uv",
  ["run", "python", "-m", "app.main", "--host", host, "--port", port],
  {
    cwd: backendRoot,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
    shell: isWindows,
    stdio: "inherit",
  },
);

child.on("exit", (code) => {
  process.exit(code ?? 0);
});

child.on("error", (error) => {
  console.error(`[backend:dev] Failed to start uv: ${error.message}`);
  process.exit(1);
});
