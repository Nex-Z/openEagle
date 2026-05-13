import { useMemo } from "react";
import { X } from "lucide-react";
import { executionStateLabel } from "../../lib/runLabels";

interface SoloOverlayProps {
  title: string;
  detail: string;
  stepText: string;
  historyText: string;
  state: string;
  stepCount: number;
  maxSteps: number;
  onDismiss: () => void;
}

export function SoloOverlay(props: SoloOverlayProps) {
  const { title, detail, stepText, historyText, state, stepCount, maxSteps, onDismiss } = props;

  const historyLines = useMemo(
    () =>
      historyText
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .slice(-3),
    [historyText],
  );

  const progress = maxSteps ? Math.min((stepCount / maxSteps) * 100, 100) : 0;

  const isActive =
    state === "running" || state === "paused" || state === "waiting_user_confirmation";

  if (state === "idle") {
    return null;
  }

  return (
    <div className="solo-overlay-root">
      <section className={`solo-overlay-card tone-${state}`}>
        <header className="solo-overlay-header">
          <div>
            <p>桌面执行</p>
            <strong>{title}</strong>
          </div>
          <div className="solo-overlay-header-actions">
            <span className="solo-overlay-state">{executionStateLabel(state)}</span>
            <button
              className="solo-overlay-dismiss"
              onClick={onDismiss}
              type="button"
              title="关闭浮窗"
            >
              <X size={14} />
            </button>
          </div>
        </header>

        <div className="solo-overlay-progress">
          <div className="solo-overlay-progress-track">
            <span style={{ width: `${progress}%` }} />
          </div>
          <small>
            {stepCount}/{maxSteps}
          </small>
        </div>

        <p className="solo-overlay-detail">{detail}</p>

        <div className="solo-overlay-current">
          <span>当前</span>
          <strong>{stepText}</strong>
        </div>

        {historyLines.length > 0 ? (
          <div className="solo-overlay-history">
            {historyLines.map((line) => (
              <span key={line}>{line}</span>
            ))}
          </div>
        ) : null}

        {isActive ? <div className="solo-overlay-pulse" /> : null}
      </section>
    </div>
  );
}
