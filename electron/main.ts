import { tmpdir } from "node:os";
import { ipcMain } from "electron";
import { attachFixtureBackend } from "./fixture-backend";
import { app, BrowserWindow, protocol, screen, globalShortcut } from "electron";
import path from "node:path";
import fs from "node:fs";
import { spawnBackend, killBackend, getBackendState, onBackendStateChange } from "./backend-manager";
import { registerIpcHandlers } from "./ipc-handlers";
import { initLogPath, emitPrefixedLog } from "./log";
import { hideSoloOverlay } from "./overlay";
import {
  configureQuickAssistant,
  hideQuickAssistant,
  preloadQuickAssistant,
  registerQuickAssistantIpc,
  registerQuickAssistantShortcut,
} from "./quick-assistant";
import { loadAppSettings, saveAppSettings } from "./conversations";
import { migrateTauriSettings } from "./migrate-tauri-settings";

const isDev = !app.isPackaged;
const guiMode = isDev && process.env.OPEN_EAGLE_V2_GUI === "1";
const piMode = process.env.OPEN_EAGLE_V2_PI === "1";
const v2Mode = guiMode || piMode || (isDev && process.env.OPEN_EAGLE_V2_FIXTURE === "1");

// 禁用 GPU 硬件加速，避免部分 Windows 设备上 GPU 进程崩溃
// 注意：命令行开关需在 electron-dev.cjs / 启动脚本中传入，此处仅作打包后的兜底
app.disableHardwareAcceleration();

// Match Tauri's app data directory for conversation persistence
app.setName("com.openeagle.desktop");
if (process.platform === "win32") {
  app.setAppUserModelId("com.openeagle.desktop");
}

if (v2Mode) app.setPath("userData", fs.mkdtempSync(path.join(tmpdir(), "openeagle-v2-app-")));

let mainWindow: BrowserWindow | null = null;
let stopFixture: (() => Promise<void>) | undefined;
let fixtureQuitReady = false;
let fixtureQuitting = false;

interface QuickAssistantStartupSettings {
  enabled?: boolean;
  hotkey?: string;
  autoReadSelection?: boolean;
}

function quickAssistantSettingsFrom(settings: unknown): QuickAssistantStartupSettings | undefined {
  if (!settings || typeof settings !== "object") {
    return undefined;
  }
  const quickAssistant = (settings as { quickAssistant?: unknown }).quickAssistant;
  if (!quickAssistant || typeof quickAssistant !== "object") {
    return undefined;
  }
  const candidate = quickAssistant as Record<string, unknown>;
  return {
    enabled: typeof candidate.enabled === "boolean" ? candidate.enabled : undefined,
    hotkey: typeof candidate.hotkey === "string" ? candidate.hotkey : undefined,
    autoReadSelection:
      typeof candidate.autoReadSelection === "boolean" ? candidate.autoReadSelection : undefined,
  };
}

function appIconPath(): string {
  return path.resolve(
    __dirname,
    process.platform === "win32" ? "../build/icon.ico" : "../build/icon.png",
  );
}

function createMainWindow() {
  const display = screen.getPrimaryDisplay();
  const width = Math.min(1440, display.workArea.width);
  const height = Math.min(920, display.workArea.height);

  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: 1080,
    minHeight: 720,
    title: "openEagle",
    icon: appIconPath(),
    autoHideMenuBar: true,
    show: false, // show after backend ready
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      additionalArguments: v2Mode ? [guiMode ? "--openeagle-v2-gui" : piMode ? "--openeagle-v2-pi" : "--openeagle-v2-fixture", ...(process.env.OPEN_EAGLE_V2_PI_TEST === "1" ? ["--openeagle-v2-offline"] : [])] : [],
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: v2Mode, // legacy asset behavior is retained outside fixture mode
    },
  });

  // 安全发送 IPC，渲染帧已销毁时静默忽略
  function safeSend(channel: string, ...args: unknown[]) {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send(channel, ...args);
      }
    } catch {
      // 渲染帧已销毁时忽略
    }
  }

  // Handle close request (equivalent to Tauri CloseRequested)
  mainWindow.on("close", () => {
    safeSend("app:close-requested");
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    hideSoloOverlay();
    hideQuickAssistant(true);
    killBackend();
    app.quit();
  });

  mainWindow.on("focus", () => {
    safeSend("main://focus_changed", true);
  });

  mainWindow.on("blur", () => {
    safeSend("main://focus_changed", false);
  });

  if (isDev) {
    mainWindow.loadURL("http://127.0.0.1:1420");
  } else {
    mainWindow.loadFile(path.resolve(__dirname, "../dist/index.html"));
  }

  return mainWindow;
}

// Register asset:// protocol to serve local files (replaces Tauri convertFileSrc)
function registerAssetProtocol() {
  protocol.handle("asset", (request) => {
    const url = new URL(request.url);
    const filePath = decodeURIComponent(url.pathname);
    // On Windows, pathname starts with /C:/... so strip leading /
    const normalizedPath = process.platform === "win32" && filePath.startsWith("/")
      ? filePath.slice(1)
      : filePath;

    if (!fs.existsSync(normalizedPath)) {
      return new Response("File not found", { status: 404 });
    }

    const ext = path.extname(normalizedPath).toLowerCase();
    const mimeMap: Record<string, string> = {
      ".png": "image/png",
      ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg",
      ".gif": "image/gif",
      ".webp": "image/webp",
      ".bmp": "image/bmp",
      ".svg": "image/svg+xml",
      ".ico": "image/x-icon",
      ".json": "application/json",
      ".html": "text/html",
      ".css": "text/css",
      ".js": "application/javascript",
    };
    const contentType = mimeMap[ext] ?? "application/octet-stream";
    const data = fs.readFileSync(normalizedPath);
    return new Response(data, {
      headers: { "Content-Type": contentType },
    });
  });
}

app.whenReady().then(async () => {
  if (!v2Mode) registerAssetProtocol();
  registerIpcHandlers(isDev);
  if (v2Mode) {
    ipcMain.removeHandler("get_backend_state");
    ipcMain.handle("get_backend_state", event => {
      if (event.sender !== mainWindow?.webContents || event.senderFrame !== mainWindow.webContents.mainFrame) throw new Error("Invalid fixture sender");
      return {phase:"ready",port:1,message:piMode?"V2 Pi experimental session":"V2 TEST MODE · fake broker"};
    });
    const win = createMainWindow();
    stopFixture = attachFixtureBackend(win,{mode:guiMode?"gui":piMode?"pi":"fixture",offline:piMode&&process.env.OPEN_EAGLE_V2_PI_TEST === "1"});
    win.once("ready-to-show", () => { if (process.env.OPENEAGLE_FIXTURE_HEADLESS !== "1") win.show(); });
    return;
  }
  registerQuickAssistantIpc(isDev, () => mainWindow);

  initLogPath(isDev);
  emitPrefixedLog(false, "[APP]", "Electron app starting");

  // One-time migration: import Tauri settings if settings file doesn't exist
  const existingSettings = loadAppSettings();
  let startupQuickAssistantSettings = quickAssistantSettingsFrom(existingSettings);
  if (!existingSettings) {
    const tauriSettings = await migrateTauriSettings();
    if (tauriSettings) {
      saveAppSettings(tauriSettings);
      startupQuickAssistantSettings = quickAssistantSettingsFrom(tauriSettings);
      emitPrefixedLog(false, "[APP]", "Migrated settings from Tauri webview");
    }
  }

  const win = createMainWindow();
  if (startupQuickAssistantSettings) {
    configureQuickAssistant(startupQuickAssistantSettings, isDev);
  } else {
    registerQuickAssistantShortcut(isDev);
  }
  setTimeout(() => {
    preloadQuickAssistant(isDev);
  }, 1200);

  // Show window once backend is connected (or on error after 15s)
  let windowShown = false;
  const showWindow = () => {
    if (!windowShown && win && !win.isDestroyed()) {
      windowShown = true;
      win.show();
    }
  };

  onBackendStateChange((state) => {
    if (state.phase === "ready") {
      showWindow();
    }
    if (state.phase === "error") {
      setTimeout(showWindow, 3000);
    }
  });

  // Fallback: show window after 15 seconds even if backend hasn't started
  setTimeout(showWindow, 15_000);

  spawnBackend(win, isDev);
});

app.on("window-all-closed", () => {
  hideSoloOverlay();
  hideQuickAssistant(true);
  killBackend();
  app.quit();
});

app.on("before-quit", (event) => {
  if (v2Mode && !fixtureQuitReady) {
    event.preventDefault();
    if (!fixtureQuitting) {
      fixtureQuitting = true;
      void (stopFixture?.() ?? Promise.resolve()).finally(() => { fixtureQuitReady = true; app.quit(); });
    }
    return;
  }
  globalShortcut.unregisterAll();
  hideSoloOverlay();
  hideQuickAssistant(true);
  killBackend();
});
