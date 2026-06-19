import { contextBridge, ipcRenderer } from "electron";

const ALLOWED_INVOKE_CHANNELS = [
  "get_backend_state",
  "write_frontend_log",
  "load_conversation_index",
  "load_conversation_file",
  "save_conversation_file",
  "save_conversation_index",
  "delete_conversation_file",
  "load_solo_run_log",
  "capture_screenshot",
  "read_image_data_url",
  "perform_mouse_action",
  "perform_keyboard_action",
  "show_solo_overlay",
  "update_solo_overlay",
  "hide_solo_overlay",
  "solo_overlay_ready",
  "show_solo_target_highlight",
  "hide_solo_target_highlight",
  "notify_solo_result",
  "load_app_settings",
  "save_app_settings",
  "show_quick_assistant",
  "hide_quick_assistant",
  "configure_quick_assistant",
  "quick_assistant_ready",
  "read_active_selection",
  "capture_context_screenshot",
  "update_quick_assistant",
];

const ALLOWED_LISTEN_CHANNELS = [
  "backend://status",
  "main://focus_changed",
  "solo://overlay_state",
  "solo://overlay_control",
  "solo://user_dismissed",
  "quick://context",
  "quick://state",
  "quick://submit",
];

const ALLOWED_SEND_CHANNELS = [
  "solo:user-dismissed",
  "solo:overlay-control",
  "solo:overlay-layout",
  "quick:submit",
  "quick:dismiss",
  "quick:open-main",
  "quick:screenshot-selection",
  "quick:screenshot-cancel",
];

contextBridge.exposeInMainWorld("electronAPI", {
  invoke: (channel: string, args?: Record<string, unknown>) => {
    if (!ALLOWED_INVOKE_CHANNELS.includes(channel)) {
      return Promise.reject(new Error(`Blocked IPC invoke: ${channel}`));
    }
    // Pass args as a single object (Tauri-compatible pattern)
    return ipcRenderer.invoke(channel, args);
  },

  on: (channel: string, callback: (...args: unknown[]) => void): (() => void) => {
    if (!ALLOWED_LISTEN_CHANNELS.includes(channel)) {
      console.warn(`Blocked IPC listen: ${channel}`);
      return () => {};
    }
    const subscription = (_event: Electron.IpcRendererEvent, ...args: unknown[]) => {
      // Wrap in { payload } to match Tauri event shape for backward compatibility
      if (args.length === 1 && typeof args[0] === "object" && args[0] !== null) {
        callback({ payload: args[0] });
      } else {
        callback({ payload: args[0] });
      }
    };
    ipcRenderer.on(channel, subscription);
    return () => {
      ipcRenderer.removeListener(channel, subscription);
    };
  },

  emit: (channel: string, ...args: unknown[]) => {
    if (!ALLOWED_SEND_CHANNELS.includes(channel)) {
      console.warn(`Blocked IPC emit: ${channel}`);
      return;
    }
    ipcRenderer.send(channel, ...args);
  },

  convertFileSrc: (filePath: string) => {
    // Encode file path for use as asset:// protocol URL
    const normalized = filePath.replace(/\\/g, "/");
    return `asset:///${encodeURI(normalized)}`;
  },

  onCloseRequested: (callback: () => void) => {
    ipcRenderer.on("app:close-requested", () => {
      callback();
    });
    return () => {
      ipcRenderer.removeAllListeners("app:close-requested");
    };
  },

  isElectron: true,
});
