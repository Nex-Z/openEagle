// scripts/ensure-cjs-package.js
// Creates dist-electron/package.json with {"type":"commonjs"} for Electron
const fs = require("fs");
const path = require("path");
const dir = path.resolve(__dirname, "..", "dist-electron");
fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(dir, "package.json"), '{"type":"commonjs"}\n');
