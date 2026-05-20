import { app, BrowserWindow, protocol, screen } from "electron";
import path from "node:path";
import fs from "node:fs";
import { spawnBackend, killBackend, getBackendState, onBackendStateChange } from "./backend-manager";
import { registerIpcHandlers } from "./ipc-handlers";
import { initLogPath, emitPrefixedLog } from "./log";
import { hideSoloOverlay } from "./overlay";

const isDev = !app.isPackaged;

// Match Tauri's app data directory for conversation persistence
app.setName("com.openeagle.desktop");

let mainWindow: BrowserWindow | null = null;

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
    autoHideMenuBar: true,
    show: false, // show after backend ready
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: false, // needed for asset:// protocol
    },
  });

  // Handle close request (equivalent to Tauri CloseRequested)
  mainWindow.on("close", () => {
    mainWindow?.webContents.send("app:close-requested");
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    hideSoloOverlay();
    killBackend();
    app.quit();
  });

  mainWindow.on("focus", () => {
    mainWindow?.webContents.send("main://focus_changed", true);
  });

  mainWindow.on("blur", () => {
    mainWindow?.webContents.send("main://focus_changed", false);
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

app.whenReady().then(() => {
  registerAssetProtocol();
  registerIpcHandlers(isDev);

  initLogPath(isDev);
  emitPrefixedLog(false, "[APP]", "Electron app starting");

  const win = createMainWindow();

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
  killBackend();
  app.quit();
});

app.on("before-quit", () => {
  hideSoloOverlay();
  killBackend();
});
