import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { emit, listen } from "@tauri-apps/api/event";
import { SoloOverlay } from "./SoloOverlay";

interface OverlayState {
  title: string;
  detail: string;
  stepText: string;
  historyText: string;
  state: string;
  stepCount: number;
  maxSteps: number;
}

declare global {
  interface Window {
    __SOLO_OVERLAY__?: OverlayState;
  }
}

const FALLBACK: OverlayState = {
  title: "SOLO 正在执行桌面操作",
  detail: "请保持桌面可见",
  stepText: "",
  historyText: "",
  state: "running",
  stepCount: 0,
  maxSteps: 100,
};

export function SoloOverlayWindow() {
  const [overlay, setOverlay] = useState<OverlayState>(
    () => window.__SOLO_OVERLAY__ ?? FALLBACK,
  );

  useEffect(() => {
    void invoke("solo_overlay_ready").catch((err: unknown) =>
      console.error("[SOLO] solo_overlay_ready failed:", err),
    );
  }, []);

  useEffect(() => {
    const unlisten = listen<OverlayState>("solo://overlay_state", (event) => {
      setOverlay(event.payload);
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  return (
    <SoloOverlay
      title={overlay.title}
      detail={overlay.detail}
      stepText={overlay.stepText}
      historyText={overlay.historyText}
      state={overlay.state}
      stepCount={overlay.stepCount}
      maxSteps={overlay.maxSteps}
      onDismiss={() => {
        void emit("solo://user_dismissed");
      }}
    />
  );
}
