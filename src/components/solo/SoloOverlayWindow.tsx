import { useEffect, useState } from "react";
import { invoke, listen } from "../../lib/electron-bridge";
import type { SoloOverlayPlanItem } from "../../types/protocol";
import { SoloOverlay } from "./SoloOverlay";

interface OverlayState {
  title: string;
  detail: string;
  stepText: string;
  stepLabel?: string;
  historyText?: string;
  state: string;
  stepCount: number;
  maxSteps: number;
  planItems?: SoloOverlayPlanItem[];
  confirmationAction?: string;
  confirmationReason?: string;
}

declare global {
  interface Window {
    __SOLO_OVERLAY__?: OverlayState;
  }
}

const FALLBACK: OverlayState = {
  title: "正在执行桌面任务",
  detail: "请保持桌面可见",
  stepText: "",
  stepLabel: "",
  historyText: "",
  state: "running",
  stepCount: 0,
  maxSteps: 100,
  planItems: [],
  confirmationAction: "",
  confirmationReason: "",
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
      state={overlay.state}
      stepCount={overlay.stepCount}
      maxSteps={overlay.maxSteps}
      stepLabel={overlay.stepLabel ?? ""}
      planItems={overlay.planItems ?? []}
      confirmationAction={overlay.confirmationAction ?? ""}
      confirmationReason={overlay.confirmationReason ?? ""}
    />
  );
}
