import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock3,
  ExternalLink,
  GripHorizontal,
  Loader2,
  Maximize2,
  Minimize2,
  PauseCircle,
  Play,
  Pause,
  Square,
  Check,
  X,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { executionStateLabel } from "../../lib/runLabels";
import type {
  SoloOverlayControlAction,
  SoloOverlayPlanItem,
} from "../../types/protocol";

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
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onControl: (action: SoloOverlayControlAction) => void;
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

interface OverlayControlButton {
  action: SoloOverlayControlAction;
  label: string;
  title: string;
  icon: LucideIcon;
  tone?: "primary" | "danger" | "warning";
}

function controlButtonsFor(state: string): OverlayControlButton[] {
  if (state === "waiting_user_confirmation") {
    return [
      {
        action: "confirm_allow",
        label: "同意",
        title: "同意当前待确认动作",
        icon: Check,
        tone: "primary",
      },
      {
        action: "confirm_reject",
        label: "拒绝",
        title: "拒绝当前待确认动作",
        icon: X,
        tone: "danger",
      },
      {
        action: "stop",
        label: "结束",
        title: "结束桌面执行",
        icon: Square,
        tone: "danger",
      },
    ];
  }

  if (state === "paused") {
    return [
      {
        action: "resume",
        label: "继续",
        title: "继续桌面执行",
        icon: Play,
        tone: "primary",
      },
      {
        action: "stop",
        label: "结束",
        title: "结束桌面执行",
        icon: Square,
        tone: "danger",
      },
    ];
  }

  if (state === "running") {
    return [
      {
        action: "pause",
        label: "暂停",
        title: "暂停桌面执行",
        icon: Pause,
        tone: "warning",
      },
      {
        action: "stop",
        label: "结束",
        title: "结束桌面执行",
        icon: Square,
        tone: "danger",
      },
    ];
  }

  return [
    {
      action: "open_main",
      label: "主窗",
      title: "打开主窗口",
      icon: ExternalLink,
      tone: "primary",
    },
  ];
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
    collapsed,
    onToggleCollapsed,
    onControl,
  } = props;

  const progress = maxSteps > 0 ? Math.min((stepCount / maxSteps) * 100, 100) : 0;
  const progressLabel = maxSteps > 0 ? `${stepCount}/${maxSteps}` : `${stepCount}`;
  const visibleStep =
    stepText ||
    (state === "waiting_user_confirmation" ? "等待你确认后继续。" : "正在准备下一步。");
  const StateIcon = stateIconFor(state);
  const showCurrent =
    state !== "waiting_user_confirmation" || (!confirmationAction && !confirmationReason);
  const CollapseIcon = collapsed ? Maximize2 : Minimize2;
  const controlButtons = controlButtonsFor(state);

  if (state === "idle") {
    return null;
  }

  const controls = (
    <div className="solo-overlay-controls" aria-label="桌面执行控制">
      {controlButtons.map((control) => {
        const Icon = control.icon;
        return (
          <button
            className={`solo-overlay-control-button tone-${control.tone ?? "neutral"}`}
            key={control.action}
            onClick={() => onControl(control.action)}
            title={control.title}
            type="button"
          >
            <Icon size={13} />
            <span>{control.label}</span>
          </button>
        );
      })}
    </div>
  );

  if (collapsed) {
    return (
      <div className="solo-overlay-root is-collapsed">
        <section className={`solo-overlay-card is-collapsed tone-${state}`}>
          <header className="solo-overlay-compact-head solo-overlay-drag-region">
            <div className="solo-overlay-compact-title">
              <GripHorizontal size={12} />
              <StateIcon
                size={13}
                className={state === "running" ? "spin" : undefined}
              />
              <strong>{executionStateLabel(state)}</strong>
            </div>
            <button
              className="solo-overlay-icon-button"
              onClick={onToggleCollapsed}
              title="还原悬浮窗"
              type="button"
            >
              <CollapseIcon size={13} />
            </button>
          </header>

          <div className="solo-overlay-progress is-compact">
            <div className="solo-overlay-progress-track">
              <span style={{ width: `${progress}%` }} />
            </div>
            <small>{progressLabel}</small>
          </div>

          {controls}
        </section>
      </div>
    );
  }

  return (
    <div className="solo-overlay-root">
      <section className={`solo-overlay-card tone-${state}`}>
        <header className="solo-overlay-header">
          <div className="solo-overlay-title solo-overlay-drag-region">
            <p>
              <GripHorizontal size={12} />
              <Activity size={12} />
              桌面执行
            </p>
            <strong>{title}</strong>
          </div>
          <div className="solo-overlay-header-actions">
            <span className="solo-overlay-state">
              <StateIcon
                size={13}
                className={state === "running" ? "spin" : undefined}
              />
              {executionStateLabel(state)}
            </span>
            <button
              className="solo-overlay-icon-button"
              onClick={() => onControl("open_main")}
              title="打开主窗口"
              type="button"
            >
              <ExternalLink size={13} />
            </button>
            <button
              className="solo-overlay-icon-button"
              onClick={onToggleCollapsed}
              title="缩小悬浮窗"
              type="button"
            >
              <CollapseIcon size={13} />
            </button>
          </div>
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

        {controls}
      </section>
    </div>
  );
}
