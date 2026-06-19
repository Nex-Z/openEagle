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

// 禁用 GPU 硬件加速，避免部分 Windows 设备上 GPU 进程崩溃
// 注意：命令行开关需在 electron-dev.cjs / 启动脚本中传入，此处仅作打包后的兜底
app.disableHardwareAcceleration();

// Match Tauri's app data directory for conversation persistence
app.setName("com.openeagle.desktop");
if (process.platform === "win32") {
  app.setAppUserModelId("com.openeagle.desktop");
}

let mainWindow: BrowserWindow | null = null;

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
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: false, // needed for asset:// protocol
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
  registerAssetProtocol();
  registerIpcHandlers(isDev);
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

app.on("before-quit", () => {
  globalShortcut.unregisterAll();
  hideSoloOverlay();
  hideQuickAssistant(true);
  killBackend();
});
