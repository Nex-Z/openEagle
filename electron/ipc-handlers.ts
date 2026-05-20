import { ipcMain, Notification, BrowserWindow } from "electron";
import fs from "node:fs";
import path from "node:path";
import {
  getBackendState,
  type BackendState,
} from "./backend-manager";
import {
  loadConversationIndex,
  saveConversationIndex,
  loadConversationFile,
  saveConversationFile,
  deleteConversationFile,
  loadSoloRunLog,
} from "./conversations";
import { captureScreenshot } from "./screenshot";
import { performMouseAction, performKeyboardAction } from "./input";
import {
  showSoloOverlay,
  updateSoloOverlay,
  hideSoloOverlay,
  soloOverlayReady,
  type OverlayPayload,
} from "./overlay";
import { emitPrefixedLog } from "./log";

function readImageDataUrl(filePath: string): string {
  // Normalize POSIX paths to OS-native paths
  const normalized = filePath.replace(/\//g, path.sep);
  const target = path.resolve(normalized);
  if (!fs.existsSync(target)) {
    throw new Error(`image file does not exist: ${filePath}`);
  }
  if (!fs.statSync(target).isFile()) {
    throw new Error(`image path is not a file: ${filePath}`);
  }
  const bytes = fs.readFileSync(target);
  const ext = path.extname(target).toLowerCase().replace(".", "");
  const mimeMap: Record<string, string> = {
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    webp: "image/webp",
    gif: "image/gif",
    bmp: "image/bmp",
    png: "image/png",
  };
  const mime = mimeMap[ext] ?? "image/png";
  const base64 = bytes.toString("base64");
  return `data:${mime};base64,${base64}`;
}

function soloNotificationTitle(state: string): string {
  switch (state) {
    case "completed":
      return "桌面执行已完成";
    case "aborted":
      return "桌面执行已结束";
    case "error":
      return "桌面执行失败";
    default:
      return "桌面执行状态更新";
  }
}

function sanitizeNotificationBody(detail?: string): string {
  const raw = (detail ?? "")
    .replace(/\r/g, " ")
    .replace(/\n/g, " ")
    .trim();
  const fallback = raw || "返回 openEagle 查看执行结果。";
  const sanitized = fallback.replace(/([A-Za-z]:\\[^\s]+|\/[^\s]+)/g, "[路径]");
  if (sanitized.length <= 180) return sanitized;
  return sanitized.slice(0, 180) + "…";
}

export function registerIpcHandlers(isDev: boolean) {
  ipcMain.handle("get_backend_state", (): BackendState => {
    return getBackendState();
  });

  ipcMain.handle("write_frontend_log", (_event, args: { level?: string; message?: string }) => {
    const level = args?.level ?? "log";
    const message = args?.message ?? "";
    const normalizedLevel = ["error", "warn", "info", "log"].includes(level) ? level : "log";
    const stderr = normalizedLevel === "error" || normalizedLevel === "warn";
    const capped = message.length > 20_000 ? message.slice(0, 20_000) + "... [truncated]" : message;
    emitPrefixedLog(stderr, `[FRONTEND/${normalizedLevel}]`, capped);
    return { ok: true };
  });

  ipcMain.handle("load_conversation_index", () => {
    return loadConversationIndex();
  });

  ipcMain.handle("load_conversation_file", (_event, args: { conversationId?: string }) => {
    return loadConversationFile(args?.conversationId ?? "");
  });

  ipcMain.handle("save_conversation_file", (_event, args: { conversation?: unknown }) => {
    return saveConversationFile(args?.conversation);
  });

  ipcMain.handle("save_conversation_index", (_event, args: { index?: unknown }) => {
    return saveConversationIndex(args?.index);
  });

  ipcMain.handle("delete_conversation_file", (_event, args: { conversationId?: string }) => {
    return deleteConversationFile(args?.conversationId ?? "");
  });

  ipcMain.handle("load_solo_run_log", (_event, args: { requestId?: string }) => {
    return loadSoloRunLog(args?.requestId ?? "");
  });

  ipcMain.handle("capture_screenshot", async () => {
    console.log("[SOLO/ELECTRON] capture_screenshot");
    return await captureScreenshot();
  });

  ipcMain.handle("read_image_data_url", (_event, args: { path?: string }) => {
    return readImageDataUrl(args?.path ?? "");
  });

  ipcMain.handle("perform_mouse_action", async (_event, args: { action?: string; [key: string]: unknown }) => {
    console.log(`[SOLO/ELECTRON] perform_mouse_action action=${args?.action}`);
    return await performMouseAction(args as Parameters<typeof performMouseAction>[0]);
  });

  ipcMain.handle("perform_keyboard_action", async (_event, args: { action?: string; [key: string]: unknown }) => {
    console.log(`[SOLO/ELECTRON] perform_keyboard_action action=${args?.action}`);
    return await performKeyboardAction(args as Parameters<typeof performKeyboardAction>[0]);
  });

  ipcMain.handle("show_solo_overlay", (_event, args: { payload?: OverlayPayload }) => {
    const payload = args?.payload ?? {};
    console.log(`[SOLO/ELECTRON] show_solo_overlay state=${payload.state ?? "running"}`);
    return showSoloOverlay(payload, isDev);
  });

  ipcMain.handle("update_solo_overlay", (_event, args: { payload?: OverlayPayload }) => {
    const payload = args?.payload ?? {};
    console.log(`[SOLO/ELECTRON] update_solo_overlay state=${payload.state ?? "running"}`);
    return updateSoloOverlay(payload);
  });

  ipcMain.handle("hide_solo_overlay", () => {
    console.log("[SOLO/ELECTRON] hide_solo_overlay");
    return hideSoloOverlay();
  });

  ipcMain.handle("solo_overlay_ready", () => {
    return soloOverlayReady();
  });

  ipcMain.handle("notify_solo_result", (_event, args: { payload?: { requestId?: string; state: string; detail?: string } }) => {
    const payload = args?.payload ?? { state: "unknown" };
    const title = soloNotificationTitle(payload.state);
    const body = sanitizeNotificationBody(payload.detail);
    console.log(`[SOLO/ELECTRON] notify_solo_result state=${payload.state} body=${body}`);

    const notification = new Notification({ title, body });
    notification.show();

    return { ok: true, notified: true, requestId: payload.requestId };
  });

  // Solo overlay → main process IPC (overlay emits "solo:user-dismissed")
  ipcMain.on("solo:user-dismissed", (event) => {
    // Forward to all windows (main window listens via electronAPI.on)
    const allWindows = BrowserWindow.getAllWindows();
    for (const win of allWindows) {
      if (win.webContents.id !== event.sender.id) {
        win.webContents.send("solo://user_dismissed");
      }
    }
  });
}
