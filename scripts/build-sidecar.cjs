const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "..");
const backendRoot = path.join(projectRoot, "backend");
const binaryRoot = path.join(backendRoot, "binaries");
const targetName = process.env.OPEN_EAGLE_SIDECAR_NAME || "open-eagle-agent";

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: projectRoot,
    stdio: "inherit",
    ...options,
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

fs.mkdirSync(binaryRoot, { recursive: true });
for (const name of [targetName, `${targetName}.exe`]) {
  fs.rmSync(path.join(binaryRoot, name), { force: true });
}

run("uv", ["sync", "--project", backendRoot, "--extra", "build"]);
run("uv", [
  "run",
  "--project",
  backendRoot,
  "pyinstaller",
  "--noconfirm",
  "--onefile",
  "--name",
  targetName,
  "--distpath",
  binaryRoot,
  path.join(backendRoot, "app", "main.py"),
]);
