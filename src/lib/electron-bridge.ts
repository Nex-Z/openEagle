/**
 * Electron 桥接层 — 将 Electron IPC 封装为与 Tauri API 兼容的接口。
 * 前端代码通过此模块替代 `@tauri-apps/api/core` 和 `@tauri-apps/api/event`。

 */

export interface ElectronAPI {
  invoke: <T = unknown>(channel: string, args?: Record<string, unknown>) => Promise<T>;
  on: (channel: string, callback: (...args: unknown[]) => void) => () => void;
  emit: (channel: string, ...args: unknown[]) => void;
  convertFileSrc: (path: string) => string;
  onCloseRequested: (callback: () => void) => () => void;
  isElectron: boolean;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
  }
}

function isElectronRuntime(): boolean {
  return typeof window !== "undefined" && "electronAPI" in window && !!window.electronAPI?.isElectron;
}

/**
 * 替代 `@tauri-apps/api/core` 的 invoke。
 * 调用主进程 IPC handler，返回 Promise。
 */
export async function invoke<T = unknown>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!isElectronRuntime()) {
    throw new Error("Not in Electron runtime");
  }
  return window.electronAPI!.invoke<T>(command, args);
}

/**
 * Tauri 兼容类型别名。
 */
export type UnlistenFn = () => void;

/**
 * 替代 `@tauri-apps/api/event` 的 listen。
 * 监听主进程事件，返回取消监听的函数。
 */
export function listen<T = unknown>(
  event: string,
  handler: (event: { payload: T }) => void,
): Promise<() => void> {
  if (!isElectronRuntime()) {
    return Promise.resolve(() => {});
  }
  const unlisten = window.electronAPI!.on(event, (data: unknown) => {
    handler(data as { payload: T });
  });
  return Promise.resolve(unlisten);
}

/**
 * 替代 `@tauri-apps/api/event` 的 emit。
 * 向主进程发送事件（通过 ipcRenderer.send）。
 */
export function emit(event: string, ...args: unknown[]): void {
  if (!isElectronRuntime()) {
    return;
  }
  window.electronAPI!.emit(event, ...args);
}

/**
 * 替代 `@tauri-apps/api/core` 的 convertFileSrc。
 * 将本地文件路径转换为 asset:// URL。
 */
export function convertFileSrc(filePath: string): string {
  if (!isElectronRuntime()) {
    return filePath;
  }
  return window.electronAPI!.convertFileSrc(filePath);
}

/**
 * 替代 `@tauri-apps/api/window` 的 getCurrentWindow().onCloseRequested。
 * 监听窗口关闭请求，返回取消监听的函数。
 */
export function onCloseRequested(callback: () => void): () => void {
  if (!isElectronRuntime()) {
    return () => {};
  }
  return window.electronAPI!.onCloseRequested(callback);
}
