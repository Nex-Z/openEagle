import fs from "node:fs";
import path from "node:path";
import { app } from "electron";

let logPath: string | null = null;

export function initLogPath(isDev: boolean): string {
  const dir = isDev
    ? path.resolve(__dirname, "..", ".open-eagle", "logs")
    : path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  const date = new Date().toISOString().slice(0, 10);
  logPath = path.join(dir, `openEagle-${date}.log`);
  console.log(`[APP] log file: ${logPath}`);
  return logPath;
}

export function appendLog(line: string): void {
  if (!logPath) return;
  try {
    fs.appendFileSync(logPath, `${new Date().toISOString()} ${line}\n`);
  } catch {
    // best-effort
  }
}

export function emitPrefixedLog(stderr: boolean, prefix: string, message: string): void {
  let wroteLine = false;
  for (const line of message.split("\n")) {
    wroteLine = true;
    const entry = `${prefix} ${line}`;
    if (stderr) {
      console.error(entry);
    } else {
      console.log(entry);
    }
    appendLog(entry);
  }
  if (!wroteLine) {
    const entry = prefix;
    if (stderr) {
      console.error(entry);
    } else {
      console.log(entry);
    }
    appendLog(entry);
  }
}

export function relayBackendOutput(stream: "stdout" | "stderr", line: string): string {
  const text = line.trimEnd();
  if (text) {
    emitPrefixedLog(stream === "stderr", `[BACKEND/${stream}]`, text);
  }
  return text;
}
