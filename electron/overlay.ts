import { BrowserWindow, screen } from "electron";
import path from "node:path";

const SOLO_OVERLAY_WIDTH = 430;
const SOLO_OVERLAY_HEIGHT = 320;
const SOLO_OVERLAY_MARGIN = 18;

interface OverlayPlanItem {
  index: number;
  status: string;
  text: string;
}

export interface OverlayPayload {
  title?: string;
  detail?: string;
  stepText?: string;
  stepLabel?: string;
  historyText?: string;
  state?: string;
  stepCount?: number;
  maxSteps?: number;
  planItems?: OverlayPlanItem[];
  confirmationAction?: string;
  confirmationReason?: string;
}

let overlayWindow: BrowserWindow | null = null;

function normalizeOverlayPayload(payload: OverlayPayload): Required<OverlayPayload> {
  return {
    title: payload.title ?? "正在执行桌面任务",
    detail: payload.detail ?? "请保持桌面可见，可随时暂停或结束。",
    stepText: payload.stepText ?? "",
    stepLabel: payload.stepLabel ?? "",
    historyText: payload.historyText ?? "",
    state: payload.state ?? "running",
    stepCount: payload.stepCount ?? 0,
    maxSteps: payload.maxSteps ?? 100,
    planItems: Array.isArray(payload.planItems) ? payload.planItems : [],
    confirmationAction: payload.confirmationAction ?? "",
    confirmationReason: payload.confirmationReason ?? "",
  };
}

function positionOverlay(win: BrowserWindow) {
  const display = screen.getPrimaryDisplay();
  const workArea = display.workArea;
  const x = workArea.x + workArea.width - SOLO_OVERLAY_WIDTH - SOLO_OVERLAY_MARGIN;
  const y = workArea.y + workArea.height - SOLO_OVERLAY_HEIGHT - SOLO_OVERLAY_MARGIN;
  win.setPosition(Math.max(x, workArea.x), Math.max(y, workArea.y));
}

function getOverlayHtmlPath(isDev: boolean): string {
  if (isDev) {
    return "http://127.0.0.1:1420/solo-overlay.html";
  }
  return `file://${path.resolve(__dirname, "../dist/solo-overlay.html")}`;
}

export function showSoloOverlay(payload: OverlayPayload, isDev: boolean): { ok: boolean } {
  const normalized = normalizeOverlayPayload(payload);

  if (overlayWindow && !overlayWindow.isDestroyed()) {
    positionOverlay(overlayWindow);
    overlayWindow.show();
    overlayWindow.webContents.send("solo://overlay_state", normalized);
    return { ok: true };
  }

  overlayWindow = new BrowserWindow({
    width: SOLO_OVERLAY_WIDTH,
    height: SOLO_OVERLAY_HEIGHT,
    alwaysOnTop: true,
    frame: false,
    skipTaskbar: true,
    resizable: false,
    focusable: false,
    show: false,
    transparent: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  positionOverlay(overlayWindow);
  overlayWindow.setIgnoreMouseEvents(true, { forward: true });

  const url = getOverlayHtmlPath(isDev);
  if (url.startsWith("http")) {
    overlayWindow.loadURL(url);
  } else {
    overlayWindow.loadFile(url.replace("file://", ""));
  }

  // Inject initial state after page loads
  overlayWindow.webContents.on("did-finish-load", () => {
    overlayWindow?.webContents.send("solo://overlay_state", normalized);
  });

  return { ok: true };
}

export function updateSoloOverlay(payload: OverlayPayload): { ok: boolean } {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    const normalized = normalizeOverlayPayload(payload);
    positionOverlay(overlayWindow);
    overlayWindow.webContents.send("solo://overlay_state", normalized);
    overlayWindow.setIgnoreMouseEvents(true, { forward: true });
  }
  return { ok: true };
}

export function hideSoloOverlay(): { ok: boolean } {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.hide();
    overlayWindow.close();
  }
  overlayWindow = null;
  return { ok: true };
}

export function soloOverlayReady(): { ok: boolean } {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.show();
  }
  return { ok: true };
}
