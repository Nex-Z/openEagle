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

export interface TargetHighlightPayload {
  x?: number;
  y?: number;
  label?: string;
  displayIndex?: number;
}

let overlayWindow: BrowserWindow | null = null;
let overlayCollapsed = false;
let targetHighlightWindow: BrowserWindow | null = null;
let targetHighlightTimer: ReturnType<typeof setTimeout> | null = null;

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

function targetHighlightHtml(): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: transparent;
      pointer-events: none;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #marker {
      position: absolute;
      left: 0;
      top: 0;
      width: 34px;
      height: 34px;
      border-radius: 999px;
      border: 3px solid #5b8cff;
      background: rgba(91, 140, 255, 0.18);
      box-shadow: 0 0 0 12px rgba(91, 140, 255, 0.14), 0 0 28px rgba(91, 140, 255, 0.7);
      transform: translate(-50%, -50%) scale(0.86);
      opacity: 0;
      transition: opacity 120ms ease, transform 180ms ease;
    }
    #marker.is-visible {
      opacity: 1;
      transform: translate(-50%, -50%) scale(1);
      animation: pulse 900ms ease-out infinite;
    }
    #marker::after {
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: inherit;
      background: #5b8cff;
    }
    #label {
      position: absolute;
      left: 28px;
      top: 50%;
      max-width: 260px;
      transform: translateY(-50%);
      padding: 6px 10px;
      border-radius: 999px;
      color: #ffffff;
      background: rgba(20, 24, 36, 0.86);
      border: 1px solid rgba(255, 255, 255, 0.18);
      font-size: 12px;
      line-height: 1.3;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #label:empty {
      display: none;
    }
    @keyframes pulse {
      0% { box-shadow: 0 0 0 8px rgba(91, 140, 255, 0.2), 0 0 28px rgba(91, 140, 255, 0.7); }
      100% { box-shadow: 0 0 0 24px rgba(91, 140, 255, 0), 0 0 28px rgba(91, 140, 255, 0.25); }
    }
  </style>
</head>
<body>
  <div id="marker"><span id="label"></span></div>
  <script>
    window.__setTargetHighlight = function(payload) {
      const marker = document.getElementById("marker");
      const label = document.getElementById("label");
      marker.style.left = payload.x + "px";
      marker.style.top = payload.y + "px";
      label.textContent = payload.label || "";
      marker.classList.add("is-visible");
    };
  </script>
</body>
</html>`;
}

function sendTargetHighlightPayload(
  win: BrowserWindow,
  payload: Required<Pick<TargetHighlightPayload, "x" | "y">> & TargetHighlightPayload,
  bounds: Rectangle,
) {
  const label = String(payload.label ?? "").replace(/\s+/g, " ").trim().slice(0, 80);
  const relativePayload = {
    x: Math.round(payload.x - bounds.x),
    y: Math.round(payload.y - bounds.y),
    label,
  };
  void win.webContents
    .executeJavaScript(`window.__setTargetHighlight(${JSON.stringify(relativePayload)})`)
    .catch((err) => console.error("[SOLO/ELECTRON] target highlight render failed:", err));
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
  hideSoloTargetHighlight();
  return { ok: true };
}

export function soloOverlayReady(): { ok: boolean } {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.showInactive();
  }
  return { ok: true };
}

export function showSoloTargetHighlight(payload: TargetHighlightPayload): { ok: boolean } {
  const x = Number(payload.x);
  const y = Number(payload.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    hideSoloTargetHighlight();
    return { ok: false };
  }

  const display = screen.getDisplayNearestPoint({ x: Math.round(x), y: Math.round(y) });
  const bounds = display.bounds;
  const normalized = { ...payload, x, y };

  if (targetHighlightTimer) {
    clearTimeout(targetHighlightTimer);
    targetHighlightTimer = null;
  }

  if (!targetHighlightWindow || targetHighlightWindow.isDestroyed()) {
    targetHighlightWindow = new BrowserWindow({
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      alwaysOnTop: true,
      frame: false,
      focusable: false,
      skipTaskbar: true,
      resizable: false,
      movable: false,
      transparent: true,
      hasShadow: false,
      show: false,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    targetHighlightWindow.setIgnoreMouseEvents(true, { forward: true });
    targetHighlightWindow.setAlwaysOnTop(true, "screen-saver");
    targetHighlightWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(targetHighlightHtml())}`);
    targetHighlightWindow.webContents.on("did-finish-load", () => {
      if (!targetHighlightWindow || targetHighlightWindow.isDestroyed()) {
        return;
      }
      targetHighlightWindow.setBounds(bounds);
      targetHighlightWindow.showInactive();
      sendTargetHighlightPayload(targetHighlightWindow, normalized, bounds);
    });
  } else {
    targetHighlightWindow.setBounds(bounds);
    targetHighlightWindow.showInactive();
    sendTargetHighlightPayload(targetHighlightWindow, normalized, bounds);
  }

  targetHighlightTimer = setTimeout(() => {
    hideSoloTargetHighlight();
  }, 1800);

  return { ok: true };
}

export function hideSoloTargetHighlight(): { ok: boolean } {
  if (targetHighlightTimer) {
    clearTimeout(targetHighlightTimer);
    targetHighlightTimer = null;
  }
  if (targetHighlightWindow && !targetHighlightWindow.isDestroyed()) {
    targetHighlightWindow.hide();
  }
  return { ok: true };
}
