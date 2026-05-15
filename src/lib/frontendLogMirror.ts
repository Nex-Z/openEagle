import { invoke } from "@tauri-apps/api/core";

type ConsoleLevel = "log" | "info" | "warn" | "error";

declare global {
  interface Window {
    __OPEN_EAGLE_LOG_MIRROR_INSTALLED__?: boolean;
  }
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function serializeConsoleArg(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value instanceof Error) {
    return value.stack ?? `${value.name}: ${value.message}`;
  }
  if (value === undefined) {
    return "undefined";
  }

  const seen = new WeakSet<object>();
  try {
    const serialized = JSON.stringify(value, (_key, item) => {
      if (typeof item === "object" && item !== null) {
        if (seen.has(item)) {
          return "[Circular]";
        }
        seen.add(item);
      }
      return item;
    });
    return serialized ?? String(value);
  } catch {
    return String(value);
  }
}

function mirrorFrontendLog(level: ConsoleLevel, args: unknown[]) {
  const message = args.map(serializeConsoleArg).join(" ");
  void invoke("write_frontend_log", { level, message }).catch(() => {});
}

export function installFrontendLogMirror() {
  if (!isTauriRuntime() || window.__OPEN_EAGLE_LOG_MIRROR_INSTALLED__) {
    return;
  }
  window.__OPEN_EAGLE_LOG_MIRROR_INSTALLED__ = true;

  const levels: ConsoleLevel[] = ["log", "info", "warn", "error"];
  const originals = levels.reduce(
    (acc, level) => {
      acc[level] = console[level].bind(console);
      return acc;
    },
    {} as Record<ConsoleLevel, (...args: unknown[]) => void>,
  );

  for (const level of levels) {
    console[level] = (...args: unknown[]) => {
      originals[level](...args);
      mirrorFrontendLog(level, args);
    };
  }

  window.addEventListener("error", (event) => {
    mirrorFrontendLog("error", [
      `window error: ${event.message}`,
      `${event.filename}:${event.lineno}:${event.colno}`,
      event.error,
    ]);
  });

  window.addEventListener("unhandledrejection", (event) => {
    mirrorFrontendLog("error", ["unhandled rejection:", event.reason]);
  });
}
