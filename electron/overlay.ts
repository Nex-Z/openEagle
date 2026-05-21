import { BrowserWindow, screen, type Rectangle } from "electron";
import path from "node:path";

const SOLO_OVERLAY_WIDTH = 450;
const SOLO_OVERLAY_HEIGHT = 372;
const SOLO_OVERLAY_COLLAPSED_WIDTH = 268;
const SOLO_OVERLAY_COLLAPSED_HEIGHT = 128;
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
let overlayCollapsed = false;

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

function clampOverlayBounds(bounds: Rectangle): Rectangle {
  const display = screen.getPrimaryDisplay();
  const workArea = display.workArea;
  const width = Math.min(bounds.width, workArea.width);
  const height = Math.min(bounds.height, workArea.height);
  return {
    width,
    height,
    x: Math.min(Math.max(bounds.x, workArea.x), workArea.x + workArea.width - width),
    y: Math.min(Math.max(bounds.y, workArea.y), workArea.y + workArea.height - height),
  };
}

function positionOverlay(win: BrowserWindow) {
  const display = screen.getPrimaryDisplay();
  const workArea = display.workArea;
  const width = overlayCollapsed ? SOLO_OVERLAY_COLLAPSED_WIDTH : SOLO_OVERLAY_WIDTH;
  const height = overlayCollapsed ? SOLO_OVERLAY_COLLAPSED_HEIGHT : SOLO_OVERLAY_HEIGHT;
  const x = workArea.x + workArea.width - width - SOLO_OVERLAY_MARGIN;
  const y = workArea.y + workArea.height - height - SOLO_OVERLAY_MARGIN;
  win.setBounds(clampOverlayBounds({ x, y, width, height }));
}

function resizeOverlayWindow(collapsed: boolean) {
  if (!overlayWindow || overlayWindow.isDestroyed()) {
    overlayCollapsed = collapsed;
    return;
  }

  overlayCollapsed = collapsed;
  const current = overlayWindow.getBounds();
  const width = collapsed ? SOLO_OVERLAY_COLLAPSED_WIDTH : SOLO_OVERLAY_WIDTH;
  const height = collapsed ? SOLO_OVERLAY_COLLAPSED_HEIGHT : SOLO_OVERLAY_HEIGHT;
  overlayWindow.setBounds(
    clampOverlayBounds({
      x: current.x + current.width - width,
      y: current.y + current.height - height,
      width,
      height,
    }),
  );
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
    overlayWindow.showInactive();
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
    focusable: true,
    show: false,
    transparent: true,
    acceptFirstMouse: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  positionOverlay(overlayWindow);

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
    overlayWindow.webContents.send("solo://overlay_state", normalized);
  }
  return { ok: true };
}

export function setSoloOverlayCollapsed(collapsed: boolean): { ok: boolean; collapsed: boolean } {
  resizeOverlayWindow(collapsed);
  return { ok: true, collapsed };
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
    overlayWindow.showInactive();
  }
  return { ok: true };
}
