import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock3,
  Loader2,
  PauseCircle,
  XCircle,
} from "lucide-react";
import { executionStateLabel } from "../../lib/runLabels";
import type { SoloOverlayPlanItem } from "../../types/protocol";

interface SoloOverlayProps {
  title: string;
  detail: string;
  stepText: string;
  stepLabel: string;
  state: string;
  stepCount: number;
  maxSteps: number;
  planItems: SoloOverlayPlanItem[];
  confirmationAction: string;
  confirmationReason: string;
}

const PLAN_STATUS_LABELS: Record<SoloOverlayPlanItem["status"], string> = {
  completed: "完成",
  in_progress: "当前",
  pending: "待执行",
  failed: "失败",
  skipped: "跳过",
};

function stateIconFor(state: string) {
  switch (state) {
    case "completed":
      return CheckCircle2;
    case "paused":
      return PauseCircle;
    case "waiting_user_confirmation":
      return AlertTriangle;
    case "error":
    case "aborted":
      return XCircle;
    default:
      return Loader2;
  }
}

function planIconFor(status: SoloOverlayPlanItem["status"]) {
  switch (status) {
    case "completed":
      return CheckCircle2;
    case "in_progress":
      return Loader2;
    case "failed":
      return XCircle;
    case "skipped":
      return PauseCircle;
    default:
      return Circle;
  }
}

export function SoloOverlay(props: SoloOverlayProps) {
  const {
    title,
    detail,
    stepText,
    stepLabel,
    state,
    stepCount,
    maxSteps,
    planItems,
    confirmationAction,
    confirmationReason,
  } = props;

  const progress = maxSteps > 0 ? Math.min((stepCount / maxSteps) * 100, 100) : 0;
  const progressLabel = maxSteps > 0 ? `${stepCount}/${maxSteps}` : `${stepCount}`;
  const visibleStep =
    stepText ||
    (state === "waiting_user_confirmation" ? "等待你确认后继续。" : "正在准备下一步。");
  const StateIcon = stateIconFor(state);
  const showCurrent =
    state !== "waiting_user_confirmation" || (!confirmationAction && !confirmationReason);

  if (state === "idle") {
    return null;
  }

  return (
    <div className="solo-overlay-root">
      <section className={`solo-overlay-card tone-${state}`}>
        <header className="solo-overlay-header">
          <div className="solo-overlay-title">
            <p>
              <Activity size={12} />
              桌面执行
            </p>
            <strong>{title}</strong>
          </div>
          <span className="solo-overlay-state">
            <StateIcon
              size={13}
              className={state === "running" ? "spin" : undefined}
            />
            {executionStateLabel(state)}
          </span>
        </header>

        <div className="solo-overlay-progress">
          <div className="solo-overlay-progress-track">
            <span style={{ width: `${progress}%` }} />
          </div>
          <small>{progressLabel}</small>
        </div>

        <p className="solo-overlay-detail">{detail}</p>

        {showCurrent ? (
          <div className="solo-overlay-current">
            <span>
              <Clock3 size={12} />
              {stepLabel || "当前步骤"}
            </span>
            <strong>{visibleStep}</strong>
          </div>
        ) : null}

        {state === "waiting_user_confirmation" && (confirmationAction || confirmationReason) ? (
          <div className="solo-overlay-confirmation">
            <span>{confirmationAction || "等待确认"}</span>
            <strong>{confirmationReason || "请回到 openEagle 确认后继续。"}</strong>
          </div>
        ) : null}

        {planItems.length > 0 ? (
          <div className="solo-overlay-plan" aria-label="执行计划">
            <div className="solo-overlay-plan-head">
              <span>计划步骤</span>
              <small>当前片段</small>
            </div>
            <div className="solo-overlay-plan-list">
              {planItems.map((item) => {
                const PlanIcon = planIconFor(item.status);
                return (
                  <div
                    className={`solo-overlay-plan-item is-${item.status}`}
                    key={`${item.index}-${item.status}-${item.text}`}
                  >
                    <PlanIcon
                      size={13}
                      className={item.status === "in_progress" ? "spin" : undefined}
                    />
                    <span>{item.text}</span>
                    <small>{PLAN_STATUS_LABELS[item.status]}</small>
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
