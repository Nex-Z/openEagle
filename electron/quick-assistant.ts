import {
  BrowserWindow,
  clipboard,
  desktopCapturer,
  globalShortcut,
  ipcMain,
  nativeImage,
  screen,
  type Rectangle,
} from "electron";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const QUICK_WIDTH = 420;
const QUICK_HEIGHT = 320;
const QUICK_EXPANDED_WIDTH = 560;
const QUICK_EXPANDED_HEIGHT = 520;
const QUICK_MIN_WIDTH = 360;
const QUICK_MIN_HEIGHT = 260;
const DEFAULT_QUICK_CONFIG: QuickAssistantConfig = {
  enabled: true,
  hotkey: "Control+Alt+Space",
  autoReadSelection: true,
};

interface QuickAssistantConfig {
  enabled: boolean;
  hotkey: string;
  autoReadSelection: boolean;
}

interface QuickContextPayload {
  selectionText?: string;
  capturedAt?: string;
  reset?: boolean;
}

interface QuickRuntimeState {
  quickRequestId?: string;
  requestId?: string;
  status?: "idle" | "pending" | "done" | "error" | "solo";
  content?: string;
  detail?: string;
  backendReady?: boolean;
  backendDetail?: string;
  attachments?: unknown[];
}

interface ScreenshotSelectionPayload {
  displayId?: number;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

interface ScreenshotSelection {
  displayId: number;
  bounds: Rectangle;
  region: Rectangle;
}

interface ScreenshotCapturePending {
  windows: BrowserWindow[];
  resolve: (selection: ScreenshotSelection | null) => void;
}

type MainWindowGetter = () => BrowserWindow | null;

let quickAssistantWindow: BrowserWindow | null = null;
let quickExpanded = false;
let quickUserResized = false;
let quickProgrammaticResize = false;
let lastContextPayload: QuickContextPayload = { reset: true };
let lastRuntimeState: QuickRuntimeState = { status: "idle", backendReady: false };
let screenshotPending: ScreenshotCapturePending | null = null;
let getMainWindowRef: MainWindowGetter = () => null;
let quickConfig: QuickAssistantConfig = { ...DEFAULT_QUICK_CONFIG };
let registeredQuickHotkey: string | null = null;
let quickShortcutIsDev = false;

function clampBounds(bounds: Rectangle): Rectangle {
  const display = screen.getDisplayMatching(bounds);
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

function quickBounds(expanded: boolean): Rectangle {
  const point = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(point);
  const workArea = display.workArea;
  const width = expanded ? QUICK_EXPANDED_WIDTH : QUICK_WIDTH;
  const height = expanded ? QUICK_EXPANDED_HEIGHT : QUICK_HEIGHT;
  return clampBounds({
    width,
    height,
    x: workArea.x + Math.round((workArea.width - width) / 2),
    y: workArea.y + Math.round((workArea.height - height) / 2),
  });
}

function normalizeQuickConfig(config: Partial<QuickAssistantConfig>): QuickAssistantConfig {
  return {
    enabled: typeof config.enabled === "boolean" ? config.enabled : DEFAULT_QUICK_CONFIG.enabled,
    hotkey: typeof config.hotkey === "string" ? config.hotkey.trim() : DEFAULT_QUICK_CONFIG.hotkey,
    autoReadSelection:
      typeof config.autoReadSelection === "boolean"
        ? config.autoReadSelection
        : DEFAULT_QUICK_CONFIG.autoReadSelection,
  };
}

function unregisterQuickShortcut() {
  if (!registeredQuickHotkey) {
    return;
  }
  globalShortcut.unregister(registeredQuickHotkey);
  registeredQuickHotkey = null;
}

function registerConfiguredQuickShortcut(isDev: boolean): { ok: boolean; registered: boolean; hotkey: string } {
  unregisterQuickShortcut();
  if (!quickConfig.enabled || !quickConfig.hotkey) {
    return { ok: true, registered: false, hotkey: quickConfig.hotkey };
  }

  let registered = false;
  try {
    registered = globalShortcut.register(quickConfig.hotkey, () => {
      void toggleQuickAssistant(isDev);
    });
  } catch (err) {
    console.warn(`[QUICK] failed to register shortcut: ${quickConfig.hotkey}`, err);
  }

  if (!registered) {
    console.warn(`[QUICK] failed to register shortcut: ${quickConfig.hotkey}`);
    return { ok: false, registered: false, hotkey: quickConfig.hotkey };
  }

  registeredQuickHotkey = quickConfig.hotkey;
  return { ok: true, registered: true, hotkey: quickConfig.hotkey };
}

export function configureQuickAssistant(
  config: Partial<QuickAssistantConfig>,
  isDev = quickShortcutIsDev,
): { ok: boolean; registered: boolean; hotkey: string } {
  quickShortcutIsDev = isDev;
  const previous = quickConfig;
  quickConfig = normalizeQuickConfig({ ...quickConfig, ...config });

  if (!quickConfig.enabled) {
    hideQuickAssistant(false);
  }

  const shouldReregister =
    previous.enabled !== quickConfig.enabled ||
    previous.hotkey !== quickConfig.hotkey ||
    (quickConfig.enabled && !registeredQuickHotkey);

  if (!shouldReregister) {
    return { ok: true, registered: Boolean(registeredQuickHotkey), hotkey: quickConfig.hotkey };
  }

  return registerConfiguredQuickShortcut(isDev);
}

function getQuickAssistantHtmlPath(isDev: boolean): string {
  if (isDev) {
    return "http://127.0.0.1:1420/quick-assistant.html";
  }
  return `file://${path.resolve(__dirname, "../dist/quick-assistant.html")}`;
}

function safeSendQuick(channel: string, payload: unknown) {
  if (!quickAssistantWindow || quickAssistantWindow.isDestroyed()) {
    return;
  }
  quickAssistantWindow.webContents.send(channel, payload);
}

function resizeQuickAssistantForState(state: QuickRuntimeState) {
  const expanded = state.status === "pending" || state.status === "done" || state.status === "error" || state.status === "solo";
  if (quickExpanded === expanded && quickAssistantWindow && !quickAssistantWindow.isDestroyed()) {
    return;
  }
  quickExpanded = expanded;
  if (!quickAssistantWindow || quickAssistantWindow.isDestroyed() || quickUserResized) {
    return;
  }
  const current = quickAssistantWindow.getBounds();
  const width = expanded ? QUICK_EXPANDED_WIDTH : QUICK_WIDTH;
  const height = expanded ? QUICK_EXPANDED_HEIGHT : QUICK_HEIGHT;
  quickProgrammaticResize = true;
  quickAssistantWindow.setBounds(
    clampBounds({
      width,
      height,
      x: current.x + Math.round((current.width - width) / 2),
      y: current.y + Math.round((current.height - height) / 2),
    }),
  );
  setTimeout(() => {
    quickProgrammaticResize = false;
  }, 0);
}

function snapshotClipboard() {
  return {
    text: clipboard.readText(),
    html: clipboard.readHTML(),
    rtf: clipboard.readRTF(),
    image: clipboard.readImage(),
  };
}

function restoreClipboard(snapshot: ReturnType<typeof snapshotClipboard>) {
  try {
    clipboard.clear();
    const data: Electron.Data = {};
    if (snapshot.text) data.text = snapshot.text;
    if (snapshot.html) data.html = snapshot.html;
    if (snapshot.rtf) data.rtf = snapshot.rtf;
    if (snapshot.image && !snapshot.image.isEmpty()) data.image = snapshot.image;
    if (Object.keys(data).length > 0) {
      clipboard.write(data);
    }
  } catch (err) {
    console.warn("[QUICK] clipboard restore failed:", err);
  }
}

async function pressCopyShortcut() {
  const { keyboard, Key } = await import("@nut-tree-fork/nut-js");
  const modifier = process.platform === "darwin" ? Key.LeftSuper : Key.LeftControl;
  await keyboard.pressKey(modifier);
  await keyboard.pressKey(Key.C);
  await keyboard.releaseKey(Key.C);
  await keyboard.releaseKey(modifier);
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function readActiveSelection(): Promise<{ ok: boolean; text: string }> {
  const original = snapshotClipboard();
  try {
    clipboard.clear();
    await pressCopyShortcut();
    await sleep(120);
    const text = clipboard.readText().replace(/\r\n/g, "\n").trim();
    return {
      ok: Boolean(text),
      text: text.slice(0, 20_000),
    };
  } catch (err) {
    console.warn("[QUICK] read active selection failed:", err);
    return { ok: false, text: "" };
  } finally {
    restoreClipboard(original);
  }
}

export function updateQuickAssistantState(state: QuickRuntimeState): { ok: boolean } {
  lastRuntimeState = { ...lastRuntimeState, ...state };
  resizeQuickAssistantForState(lastRuntimeState);
  safeSendQuick("quick://state", lastRuntimeState);
  return { ok: true };
}

export function hideQuickAssistant(destroy = false): { ok: boolean } {
  if (!quickAssistantWindow || quickAssistantWindow.isDestroyed()) {
    quickAssistantWindow = null;
    return { ok: true };
  }
  if (destroy) {
    quickAssistantWindow.close();
    quickAssistantWindow = null;
    return { ok: true };
  }
  quickAssistantWindow.hide();
  return { ok: true };
}

export function showQuickAssistant(
  context: QuickContextPayload,
  isDev: boolean,
  options: { show?: boolean; focus?: boolean } = {},
): { ok: boolean } {
  const shouldShow = options.show !== false;
  const shouldFocus = options.focus !== false;
  lastContextPayload = {
    ...context,
    capturedAt: context.capturedAt ?? new Date().toISOString(),
  };

  if (quickAssistantWindow && !quickAssistantWindow.isDestroyed()) {
    if (shouldShow) {
      quickUserResized = false;
      quickProgrammaticResize = true;
      quickAssistantWindow.setBounds(quickBounds(quickExpanded));
      setTimeout(() => {
        quickProgrammaticResize = false;
      }, 0);
      quickAssistantWindow.show();
      if (shouldFocus) {
        quickAssistantWindow.focus();
      }
    }
    safeSendQuick("quick://context", lastContextPayload);
    safeSendQuick("quick://state", lastRuntimeState);
    return { ok: true };
  }

  quickExpanded = false;
  quickUserResized = false;
  quickAssistantWindow = new BrowserWindow({
    ...quickBounds(false),
    title: "openEagle Quick Assistant",
    alwaysOnTop: true,
    frame: false,
    skipTaskbar: true,
    resizable: true,
    minWidth: QUICK_MIN_WIDTH,
    minHeight: QUICK_MIN_HEIGHT,
    focusable: true,
    show: false,
    // 不透明窗口：禁用 GPU 合成时 transparent:true 在 Windows 上失效（回退成系统底色
    // 边框），故改用不透明窗口，背景由 styles.css 的 --bg-panel 跟随主题填充。
    // backgroundColor 仅作 CSS 加载前的防闪底色（取 light 主题面板色）。
    backgroundColor: "#ffffff",
    acceptFirstMouse: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  quickAssistantWindow.on("closed", () => {
    quickAssistantWindow = null;
  });

  quickAssistantWindow.on("resize", () => {
    if (!quickProgrammaticResize) {
      quickUserResized = true;
    }
  });

  const url = getQuickAssistantHtmlPath(isDev);
  if (url.startsWith("http")) {
    quickAssistantWindow.loadURL(url);
  } else {
    quickAssistantWindow.loadFile(url.replace("file://", ""));
  }

  quickAssistantWindow.webContents.on("did-finish-load", () => {
    safeSendQuick("quick://context", lastContextPayload);
    safeSendQuick("quick://state", lastRuntimeState);
    if (shouldShow) {
      quickAssistantWindow?.show();
      if (shouldFocus) {
        quickAssistantWindow?.focus();
      }
    }
  });

  return { ok: true };
}

function openQuickAssistant(isDev: boolean): { ok: boolean } {
  if (!quickConfig.enabled) {
    return { ok: false };
  }
  const result = showQuickAssistant(
    {
      selectionText: "",
      reset: true,
      capturedAt: new Date().toISOString(),
    },
    isDev,
  );

  if (!quickConfig.autoReadSelection) {
    updateQuickAssistantState({
      status: "idle",
      detail: "",
    });
    return result;
  }

  updateQuickAssistantState({
    status: "idle",
    detail: "正在读取选中文字...",
  });

  void readActiveSelection().then((selection) => {
    showQuickAssistant(
      {
        selectionText: selection.text,
        reset: false,
        capturedAt: new Date().toISOString(),
      },
      isDev,
      { show: false },
    );
    updateQuickAssistantState({
      status: "idle",
      detail: selection.text ? "" : "未检测到选中文字。",
    });
  });

  return result;
}

export async function toggleQuickAssistant(isDev: boolean): Promise<{ ok: boolean }> {
  if (!quickConfig.enabled) {
    return { ok: false };
  }
  if (quickAssistantWindow && !quickAssistantWindow.isDestroyed() && quickAssistantWindow.isVisible()) {
    return hideQuickAssistant(false);
  }

  return openQuickAssistant(isDev);
}

export function preloadQuickAssistant(isDev: boolean): { ok: boolean } {
  if (!quickConfig.enabled) {
    return { ok: false };
  }
  if (quickAssistantWindow && !quickAssistantWindow.isDestroyed()) {
    return { ok: true };
  }
  return showQuickAssistant(
    {
      selectionText: "",
      reset: true,
      capturedAt: new Date().toISOString(),
    },
    isDev,
    { show: false },
  );
}

function selectionOverlayHtml(displayId: number): string {
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
      background: rgba(8, 15, 31, 0.18);
      cursor: crosshair;
      user-select: none;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    #hint {
      position: fixed;
      top: 18px;
      left: 50%;
      transform: translateX(-50%);
      padding: 8px 12px;
      border-radius: 999px;
      color: white;
      background: rgba(15, 23, 42, 0.82);
      border: 1px solid rgba(255, 255, 255, 0.22);
      font-size: 12px;
      pointer-events: none;
    }
    #box {
      position: fixed;
      display: none;
      border: 2px solid #5b8cff;
      background: rgba(91, 140, 255, 0.16);
      box-shadow: 0 0 0 9999px rgba(8, 15, 31, 0.22), 0 0 28px rgba(91, 140, 255, 0.36);
      pointer-events: none;
    }
  </style>
</head>
<body>
  <div id="hint">拖拽选择截图区域，Esc 取消</div>
  <div id="box"></div>
  <script>
    const displayId = ${JSON.stringify(displayId)};
    const box = document.getElementById("box");
    let start = null;
    let current = null;

    function rectFrom(a, b) {
      const x = Math.min(a.x, b.x);
      const y = Math.min(a.y, b.y);
      const width = Math.abs(a.x - b.x);
      const height = Math.abs(a.y - b.y);
      return { x, y, width, height };
    }

    function render() {
      if (!start || !current) return;
      const rect = rectFrom(start, current);
      box.style.display = "block";
      box.style.left = rect.x + "px";
      box.style.top = rect.y + "px";
      box.style.width = rect.width + "px";
      box.style.height = rect.height + "px";
    }

    window.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      start = { x: event.clientX, y: event.clientY };
      current = start;
      render();
    });

    window.addEventListener("mousemove", (event) => {
      if (!start) return;
      current = { x: event.clientX, y: event.clientY };
      render();
    });

    window.addEventListener("mouseup", (event) => {
      if (!start) return;
      current = { x: event.clientX, y: event.clientY };
      const rect = rectFrom(start, current);
      start = null;
      current = null;
      if (rect.width < 8 || rect.height < 8) {
        window.electronAPI.emit("quick:screenshot-cancel");
        return;
      }
      window.electronAPI.emit("quick:screenshot-selection", { displayId, ...rect });
    });

    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        window.electronAPI.emit("quick:screenshot-cancel");
      }
    });
  </script>
</body>
</html>`;
}

function closeScreenshotWindows() {
  const pending = screenshotPending;
  if (!pending) {
    return;
  }
  for (const win of pending.windows) {
    if (!win.isDestroyed()) {
      win.close();
    }
  }
  screenshotPending = null;
}

function normalizeSelection(payload: ScreenshotSelectionPayload): ScreenshotSelection | null {
  const displayId = Number(payload.displayId);
  const x = Number(payload.x);
  const y = Number(payload.y);
  const width = Number(payload.width);
  const height = Number(payload.height);
  if (![displayId, x, y, width, height].every(Number.isFinite) || width < 8 || height < 8) {
    return null;
  }
  const display = screen.getAllDisplays().find((item) => item.id === displayId);
  if (!display) {
    return null;
  }
  return {
    displayId,
    bounds: display.bounds,
    region: {
      x: Math.round(Math.max(0, x)),
      y: Math.round(Math.max(0, y)),
      width: Math.round(Math.min(width, display.bounds.width - x)),
      height: Math.round(Math.min(height, display.bounds.height - y)),
    },
  };
}

function createScreenshotSelectionWindows(): Promise<ScreenshotSelection | null> {
  if (screenshotPending) {
    closeScreenshotWindows();
  }
  return new Promise((resolve) => {
    const windows: BrowserWindow[] = [];
    screenshotPending = { windows, resolve };
    const cursorDisplay = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());

    for (const display of screen.getAllDisplays()) {
      const win = new BrowserWindow({
        x: display.bounds.x,
        y: display.bounds.y,
        width: display.bounds.width,
        height: display.bounds.height,
        alwaysOnTop: true,
        frame: false,
        focusable: true,
        skipTaskbar: true,
        resizable: false,
        movable: false,
        transparent: true,
        hasShadow: false,
        show: false,
        webPreferences: {
          preload: path.join(__dirname, "preload.js"),
          contextIsolation: true,
          nodeIntegration: false,
        },
      });
      win.setAlwaysOnTop(true, "screen-saver");
      win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(selectionOverlayHtml(display.id))}`);
      win.webContents.on("did-finish-load", () => {
        if (display.id === cursorDisplay.id) {
          win.show();
          win.focus();
        } else {
          win.showInactive();
        }
      });
      windows.push(win);
    }
  });
}

async function captureDisplayRegion(selection: ScreenshotSelection) {
  const scale = screen.getAllDisplays().find((display) => display.id === selection.displayId)?.scaleFactor ?? 1;
  const sources = await desktopCapturer.getSources({
    types: ["screen"],
    thumbnailSize: {
      width: Math.max(1, Math.round(selection.bounds.width * scale)),
      height: Math.max(1, Math.round(selection.bounds.height * scale)),
    },
  });
  const source =
    sources.find((item) => item.display_id === String(selection.displayId)) ??
    sources[0];
  if (!source || source.thumbnail.isEmpty()) {
    throw new Error("无法捕获当前屏幕。");
  }

  const image = source.thumbnail;
  const imageSize = image.getSize();
  const scaleX = imageSize.width / selection.bounds.width;
  const scaleY = imageSize.height / selection.bounds.height;
  const cropRect = {
    x: Math.max(0, Math.round(selection.region.x * scaleX)),
    y: Math.max(0, Math.round(selection.region.y * scaleY)),
    width: Math.max(1, Math.round(selection.region.width * scaleX)),
    height: Math.max(1, Math.round(selection.region.height * scaleY)),
  };
  const cropped = nativeImage.createFromBuffer(image.crop(cropRect).toPNG());
  const png = cropped.toPNG();
  const timestamp = Date.now();
  const name = `quick-screenshot-${timestamp}.png`;
  const targetPath = path.join(os.tmpdir(), name);
  fs.writeFileSync(targetPath, png);

  return {
    id: `quick-shot-${timestamp}`,
    name,
    mimeType: "image/png",
    size: png.length,
    kind: "image",
    source: "local",
    localPath: targetPath,
    contentBase64: `data:image/png;base64,${png.toString("base64")}`,
    status: "pending",
  };
}

export async function captureContextScreenshot(): Promise<Record<string, unknown>> {
  const wasVisible = Boolean(quickAssistantWindow && !quickAssistantWindow.isDestroyed() && quickAssistantWindow.isVisible());
  if (wasVisible) {
    quickAssistantWindow?.hide();
  }

  try {
    const selection = await createScreenshotSelectionWindows();
    closeScreenshotWindows();
    if (!selection) {
      return { ok: false, cancelled: true };
    }
    await sleep(120);
    const attachment = await captureDisplayRegion(selection);
    return { ok: true, attachment };
  } catch (err) {
    return {
      ok: false,
      error: err instanceof Error ? err.message : String(err),
    };
  } finally {
    if (wasVisible && quickAssistantWindow && !quickAssistantWindow.isDestroyed()) {
      quickAssistantWindow.show();
      quickAssistantWindow.focus();
    }
  }
}

export function registerQuickAssistantIpc(isDev: boolean, getMainWindow: MainWindowGetter) {
  getMainWindowRef = getMainWindow;

  ipcMain.handle("show_quick_assistant", () => openQuickAssistant(isDev));

  ipcMain.handle("hide_quick_assistant", () => hideQuickAssistant(false));
  ipcMain.handle("configure_quick_assistant", (_event, args: { settings?: Partial<QuickAssistantConfig> }) =>
    configureQuickAssistant(args?.settings ?? {}, isDev),
  );
  ipcMain.handle("quick_assistant_ready", () => {
    safeSendQuick("quick://context", lastContextPayload);
    safeSendQuick("quick://state", lastRuntimeState);
    return { ok: true };
  });
  ipcMain.handle("read_active_selection", () => readActiveSelection());
  ipcMain.handle("capture_context_screenshot", () => captureContextScreenshot());
  ipcMain.handle("update_quick_assistant", (_event, args: { state?: QuickRuntimeState }) =>
    updateQuickAssistantState(args?.state ?? {}),
  );

  ipcMain.on("quick:submit", (_event, payload) => {
    const mainWindow = getMainWindowRef();
    if (!mainWindow || mainWindow.isDestroyed()) {
      updateQuickAssistantState({
        status: "error",
        detail: "主窗口不可用，消息未发送。",
      });
      return;
    }
    mainWindow.webContents.send("quick://submit", payload);
  });

  ipcMain.on("quick:open-main", () => {
    const mainWindow = getMainWindowRef();
    if (!mainWindow || mainWindow.isDestroyed()) {
      return;
    }
    mainWindow.show();
    mainWindow.focus();
  });

  ipcMain.on("quick:dismiss", () => hideQuickAssistant(false));

  ipcMain.on("quick:screenshot-selection", (_event, payload: ScreenshotSelectionPayload) => {
    const pending = screenshotPending;
    if (!pending) {
      return;
    }
    const selection = normalizeSelection(payload);
    pending.resolve(selection);
  });

  ipcMain.on("quick:screenshot-cancel", () => {
    const pending = screenshotPending;
    if (pending) {
      pending.resolve(null);
    }
  });
}

export function registerQuickAssistantShortcut(isDev: boolean) {
  quickShortcutIsDev = isDev;
  return registerConfiguredQuickShortcut(isDev).registered;
}
