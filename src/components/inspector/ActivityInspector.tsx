import { useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc, invoke } from "../../lib/electron-bridge";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Image,
  ListTree,
  ShieldAlert,
  Wrench,
  X,
} from "lucide-react";
import type {
  AgentExecutionTrace,
  SoloConfirmationPayload,
  SoloPlanStatus,
  SoloStepPayload,
  SoloStatusPayload,
  SoloStepVisualPayload,
  ToolConfirmationPayload,
} from "../../types/protocol";
import { SoloPlanChecklist } from "./SoloPlanChecklist";

interface AssetMessage {
  id: string;
  imagePath: string;
  label: string;
  createdAt: string;
}

interface ActivityInspectorProps {
  traces: AgentExecutionTrace[];
  assets: AssetMessage[];
  soloStatus: SoloStatusPayload;
  soloConfirmation: SoloConfirmationPayload | null;
  soloStep: SoloStepPayload | null;
  toolConfirmation: ToolConfirmationPayload | null;
  soloTimeline: string[];
  soloLastError: string | null;
  soloPlan: SoloPlanStatus | null;
  inspectorCollapsed: boolean;
  onToggleCollapsed: () => void;
  onAllowDangerousStep: () => boolean;
  onRejectDangerousStep: () => boolean;
  onAllowToolConfirmation: () => boolean;
  onRejectToolConfirmation: () => boolean;
}

type InspectorTab = "activity" | "traces" | "assets";

function AssetImage({ src, label, onClick }: { src: string; label: string; onClick?: () => void }) {
  const [failed, setFailed] = useState(false);
  const [currentSrc, setCurrentSrc] = useState(src);

  if (src !== currentSrc) {
    setCurrentSrc(src);
    if (failed) setFailed(false);
  }

  if (failed) {
    return <div className="asset-card-fallback">图片不可用</div>;
  }

  return (
    <img
      alt={label}
      loading="lazy"
      src={src}
      onClick={onClick}
      onError={() => setFailed(true)}
    />
  );
}

function formatTraceValue(value: unknown) {
  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatTraceDuration(startedAt: string, completedAt?: string) {
  if (!completedAt) {
    return "进行中";
  }

  const duration = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(duration) || duration < 0) {
    return "刚刚";
  }
  if (duration < 1000) {
    return `${duration}ms`;
  }
  return `${(duration / 1000).toFixed(duration >= 10_000 ? 0 : 1)}s`;
}

function traceDisplayName(trace: AgentExecutionTrace) {
  const command = trace.params?.command;
  if (typeof command === "string" && command.trim()) {
    return command.trim();
  }
  return trace.name;
}

function traceStatusLabel(trace: AgentExecutionTrace) {
  switch (trace.status) {
    case "completed":
      return "完成";
    case "started":
      return "运行中";
    case "error":
      return "失败";
    default:
      return trace.status;
  }
}

function tracePlanStatus(trace: AgentExecutionTrace) {
  if (trace.status === "started") {
    return "in_progress";
  }
  if (trace.status === "error") {
    return "failed";
  }
  return "completed";
}

function normalizeImagePath(path: string) {
  return path.replace(/\\/g, "/");
}

function currentStepText(step: SoloStepPayload | null) {
  if (!step) {
    return "";
  }
  return (
    step.agentMessage ||
    step.expectedOutcome ||
    step.screenState ||
    step.visual?.displayText ||
    step.action
  ).trim();
}

function markerForAsset(asset: AssetMessage, step: SoloStepPayload | null) {
  const visual = step?.visual;
  const visualPath = visual?.screenshotPath || step?.screenshotPath;
  if (
    !visual ||
    visual.kind !== "point" ||
    !visualPath ||
    normalizeImagePath(visualPath) !== normalizeImagePath(asset.imagePath) ||
    typeof visual.screenshotX !== "number" ||
    typeof visual.screenshotY !== "number" ||
    typeof visual.screenshotWidth !== "number" ||
    typeof visual.screenshotHeight !== "number" ||
    visual.screenshotWidth <= 0 ||
    visual.screenshotHeight <= 0
  ) {
    return null;
  }

  const left = Math.min(Math.max((visual.screenshotX / visual.screenshotWidth) * 100, 0), 100);
  const top = Math.min(Math.max((visual.screenshotY / visual.screenshotHeight) * 100, 0), 100);
  return {
    left,
    top,
    label: visual.targetLabel || visual.displayText || step?.action || "target",
  };
}

function visualKindLabel(visual?: SoloStepVisualPayload) {
  switch (visual?.kind) {
    case "point":
      return "目标位置";
    case "scroll":
      return "滚动";
    case "keyboard":
      return "键盘输入";
    case "command":
      return "工具/命令";
    case "navigation":
      return "导航";
    case "wait":
      return "等待";
    default:
      return "动作";
  }
}

export function ActivityInspector(props: ActivityInspectorProps) {
  const {
    traces,
    assets,
    soloStatus,
    soloConfirmation,
    soloStep,
    toolConfirmation,
    soloTimeline,
    soloLastError,
    soloPlan,
    inspectorCollapsed,
    onToggleCollapsed,
    onAllowDangerousStep,
    onRejectDangerousStep,
    onAllowToolConfirmation,
    onRejectToolConfirmation,
  } = props;
  const [activeTab, setActiveTab] = useState<InspectorTab>("activity");
  const [expandedTraceId, setExpandedTraceId] = useState<string | null>(null);
  const [imageDataUrls, setImageDataUrls] = useState<Record<string, string>>({});
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const attemptedPathsRef = useRef<Set<string>>(new Set());
  const prevAssetsRef = useRef<typeof assets>([]);
  const stepText = currentStepText(soloStep);
  const recentTraces = useMemo(() => traces.slice(0, 6), [traces]);
  const recentToolResults = useMemo(
    () => traces.filter((trace) => trace.result || trace.params).slice(0, 3),
    [traces],
  );

  useEffect(() => {
    const imagePaths = Array.from(
      new Set(assets.map((asset) => asset.imagePath).filter(Boolean)),
    );
    const prevPaths = Array.from(
      new Set(prevAssetsRef.current.map((a) => a.imagePath).filter(Boolean)),
    );
    const assetsChanged =
      imagePaths.length !== prevPaths.length ||
      imagePaths.some((p, i) => p !== prevPaths[i]);
    if (assetsChanged) {
      prevAssetsRef.current = assets;
      attemptedPathsRef.current = new Set();
      setImageDataUrls({});
    }

    const missing = imagePaths.filter(
      (path) => !imageDataUrls[path] && !attemptedPathsRef.current.has(path),
    );
    if (missing.length === 0) {
      return;
    }

    for (const path of missing) {
      attemptedPathsRef.current.add(path);
    }

    let cancelled = false;
    void Promise.all(
      missing.map(async (path) => {
        try {
          const dataUrl = await invoke<string>("read_image_data_url", { path });
          return { path, dataUrl };
        } catch (err) {
          console.warn("[assets] read_image_data_url failed:", path, err);
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) {
        return;
      }
      const successful = entries.filter((e): e is { path: string; dataUrl: string } => e !== null);
      if (successful.length === 0) {
        return;
      }
      setImageDataUrls((current) => {
        const next = { ...current };
        for (const entry of successful) {
          next[entry.path] = entry.dataUrl;
        }
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [assets]);

  const statusTone = useMemo(() => {
    switch (soloStatus.state) {
      case "completed":
        return "success";
      case "error":
      case "aborted":
        return "danger";
      case "waiting_user_confirmation":
      case "paused":
        return "warning";
      default:
        return "neutral";
    }
  }, [soloStatus.state]);

  const hasDecisionSurface = Boolean(soloConfirmation || toolConfirmation || soloLastError);
  const confirmationCount =
    Number(Boolean(soloConfirmation)) +
    Number(Boolean(toolConfirmation)) +
    Number(Boolean(soloLastError));

  const inspector = (
    <aside
      className={inspectorCollapsed ? "activity-inspector is-collapsed" : "activity-inspector"}
    >
      <header className="inspector-header">
        <div className="inspector-header-copy">
          <p>活动</p>
          {!inspectorCollapsed ? <strong>执行轨迹</strong> : null}
        </div>
        <button className="icon-button" onClick={onToggleCollapsed} type="button">
          {inspectorCollapsed ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
        </button>
      </header>

      {inspectorCollapsed ? (
        <div className="inspector-collapsed-summary">
          <div className={`collapsed-dot tone-${statusTone}`} />
          <span>{confirmationCount}</span>
        </div>
      ) : (
        <>
          <div className="inspector-tabs" role="tablist" aria-label="活动面板标签">
            <button
              className={activeTab === "activity" ? "inspector-tab is-active" : "inspector-tab"}
              onClick={() => setActiveTab("activity")}
              type="button"
            >
              动态
            </button>
            <button
              className={activeTab === "traces" ? "inspector-tab is-active" : "inspector-tab"}
              onClick={() => setActiveTab("traces")}
              type="button"
            >
              轨迹
            </button>
            <button
              className={activeTab === "assets" ? "inspector-tab is-active" : "inspector-tab"}
              onClick={() => setActiveTab("assets")}
              type="button"
            >
              资产
            </button>
          </div>

          <div className="inspector-scroll">
            {activeTab === "activity" ? (
              <>
                {soloPlan && <SoloPlanChecklist plan={soloPlan} />}

                {!soloPlan && recentTraces.length > 0 ? (
                  <section className="inspector-card solo-plan-card derived-plan-card">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">执行计划</span>
                        <strong>
                          {recentTraces.filter((trace) => trace.status === "completed").length}/
                          {recentTraces.length} 步
                        </strong>
                      </div>
                    </div>
                    <div className="plan-checklist">
                      {recentTraces
                        .slice()
                        .reverse()
                        .map((trace) => (
                          <div
                            key={trace.id}
                            className={`plan-item plan-item-${tracePlanStatus(trace)} derived-plan-item`}
                          >
                            <span className={`trace-kind-dot trace-kind-${trace.kind}`} />
                            <span className="plan-item-desc">{traceDisplayName(trace)}</span>
                            <span className="plan-item-action">{trace.kind.toUpperCase()}</span>
                          </div>
                        ))}
                    </div>
                  </section>
                ) : null}

                {soloStep ? (
                  <section className="inspector-card current-action-card">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">当前动作</span>
                        <strong>{visualKindLabel(soloStep.visual)}</strong>
                      </div>
                      {soloStep.visual?.targetLabel ? (
                        <span className="current-action-target">{soloStep.visual.targetLabel}</span>
                      ) : null}
                    </div>
                    {stepText ? <p>{stepText}</p> : null}
                    {soloStep.visual?.safeArgsPreview ? (
                      <pre>{formatTraceValue(soloStep.visual.safeArgsPreview)}</pre>
                    ) : null}
                  </section>
                ) : null}

                {recentTraces.length > 0 ? (
                  <section className="inspector-card inspector-compact-section">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">执行轨迹</span>
                        <strong>最近调用</strong>
                      </div>
                      <ListTree size={16} />
                    </div>
                    <div className="inspector-trace-compact-list">
                      {recentTraces.map((trace) => (
                        <button
                          key={trace.id}
                          className={`inspector-trace-compact trace-status-${trace.status}`}
                          onClick={() => {
                            setActiveTab("traces");
                            setExpandedTraceId(trace.id);
                          }}
                          type="button"
                        >
                          <span className={`trace-kind-dot trace-kind-${trace.kind}`} />
                          <span className="inspector-trace-name">{traceDisplayName(trace)}</span>
                          <span className="inspector-trace-meta">
                            {traceStatusLabel(trace)} · {formatTraceDuration(trace.startedAt, trace.completedAt)}
                          </span>
                        </button>
                      ))}
                    </div>
                  </section>
                ) : null}

                {recentToolResults.length > 0 ? (
                  <section className="inspector-card inspector-compact-section">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">工具结果</span>
                        <strong>最近输出</strong>
                      </div>
                      <Wrench size={16} />
                    </div>
                    <div className="inspector-tool-result-list">
                      {recentToolResults.map((trace) => (
                        <button
                          key={trace.id}
                          className="inspector-tool-result"
                          onClick={() => {
                            setActiveTab("traces");
                            setExpandedTraceId(trace.id);
                          }}
                          type="button"
                        >
                          <span>{trace.name}</span>
                          <code>
                            {formatTraceValue(trace.result ?? trace.params).slice(0, 120)}
                          </code>
                        </button>
                      ))}
                    </div>
                  </section>
                ) : null}

                {soloTimeline.length > 0 ? (
                  <section className="inspector-card inspector-compact-section">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">事件</span>
                        <strong>最近动态</strong>
                      </div>
                      <ListTree size={16} />
                    </div>
                    <div className="timeline-list">
                      {soloTimeline
                        .slice()
                        .reverse()
                        .slice(0, 6)
                        .map((line) => (
                          <div key={line} className="timeline-item">
                            {line}
                          </div>
                        ))}
                    </div>
                  </section>
                ) : null}

                {hasDecisionSurface ? (
                  <section className="inspector-card">
                    <div className="inspector-card-head">
                      <div>
                        <span className="card-kicker">确认与异常</span>
                        <strong>需要你的决定</strong>
                      </div>
                      <ShieldAlert size={16} />
                    </div>

                    {soloConfirmation ? (
                      <div className="decision-card warning">
                        <div className="decision-card-head">
                          <ShieldAlert size={16} />
                          <strong>{soloConfirmation.action}</strong>
                        </div>
                        <p>{soloConfirmation.reason}</p>
                        {soloConfirmation.visual?.safeArgsPreview || soloConfirmation.actionArgs ? (
                          <pre>
                            {formatTraceValue(
                              soloConfirmation.visual?.safeArgsPreview ??
                                soloConfirmation.actionArgs,
                            )}
                          </pre>
                        ) : null}
                        <div className="decision-actions">
                          <button className="ghost-button" onClick={onAllowDangerousStep} type="button">
                            允许
                          </button>
                          <button
                            className="ghost-button danger"
                            onClick={onRejectDangerousStep}
                            type="button"
                          >
                            拒绝
                          </button>
                        </div>
                      </div>
                    ) : null}

                    {toolConfirmation ? (
                      <div className="decision-card danger">
                        <div className="decision-card-head">
                          <Wrench size={16} />
                          <strong>{toolConfirmation.name}</strong>
                        </div>
                        <p>{toolConfirmation.reason}</p>
                        {toolConfirmation.params ? (
                          <pre>{formatTraceValue(toolConfirmation.params)}</pre>
                        ) : null}
                        <div className="decision-actions">
                          <button className="ghost-button" onClick={onAllowToolConfirmation} type="button">
                            允许
                          </button>
                          <button
                            className="ghost-button danger"
                            onClick={onRejectToolConfirmation}
                            type="button"
                          >
                            拒绝
                          </button>
                        </div>
                      </div>
                    ) : null}

                    {soloLastError ? (
                      <div className="decision-card danger subtle">
                        <div className="decision-card-head">
                          <AlertTriangle size={16} />
                          <strong>最近异常</strong>
                        </div>
                        <p>{soloLastError}</p>
                      </div>
                    ) : null}
                  </section>
                ) : null}

                {!soloPlan &&
                !soloStep &&
                recentTraces.length === 0 &&
                recentToolResults.length === 0 &&
                soloTimeline.length === 0 &&
                !hasDecisionSurface ? (
                  <div className="inspector-quiet-state">
                    <span>执行开始后，这里会沉淀计划、调用和结果。</span>
                  </div>
                ) : null}
              </>
            ) : null}

            {activeTab === "traces" ? (
              <section className="inspector-card">
                <div className="inspector-card-head">
                  <div>
                    <span className="card-kicker">工具 / MCP / Skill</span>
                    <strong>Trace 列表</strong>
                  </div>
                  <Wrench size={16} />
                </div>
                <div className="trace-list">
                  {traces.length > 0 ? (
                    traces.map((trace) => (
                      <div
                        key={trace.id}
                        className={expandedTraceId === trace.id ? "trace-row is-expanded" : "trace-row"}
                        onClick={() =>
                          setExpandedTraceId((current) => (current === trace.id ? null : trace.id))
                        }
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setExpandedTraceId((current) =>
                              current === trace.id ? null : trace.id,
                            );
                          }
                        }}
                        role="button"
                        tabIndex={0}
                      >
                        <div className="trace-row-head">
                          <span className={`trace-chip trace-chip-${trace.kind}`}>
                            {trace.kind.toUpperCase()}
                          </span>
                          <strong>{trace.name}</strong>
                          <span className={`trace-status trace-status-${trace.status}`}>
                            {trace.status}
                          </span>
                        </div>
                        {expandedTraceId === trace.id ? (
                          <div className="trace-row-body">
                            {trace.params ? <pre>{formatTraceValue(trace.params)}</pre> : null}
                            {trace.result ? <pre>{formatTraceValue(trace.result)}</pre> : null}
                            {!trace.params && !trace.result ? (
                              <div className="trace-empty">没有额外返回数据。</div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    ))
                  ) : (
                    <div className="trace-empty">当前没有 trace 记录。</div>
                  )}
                </div>
              </section>
            ) : null}

            {activeTab === "assets" ? (
              <section className="inspector-card">
                <div className="inspector-card-head">
                  <div>
                    <span className="card-kicker">截图与素材</span>
                    <strong>执行预览</strong>
                  </div>
                  <Image size={16} />
                </div>
                <div className="asset-list">
                  {assets.length > 0 ? (
                    assets.map((asset) => {
                      const dataUrl = imageDataUrls[asset.imagePath];
                      const nativePath = asset.imagePath.replace(/\//g, "\\");
                      const fileSrc = convertFileSrc(nativePath);
                      const src = dataUrl || fileSrc;
                      const marker = markerForAsset(asset, soloStep);
                      return (
                        <figure key={asset.id} className="asset-card">
                          <div className="asset-image-wrap">
                            <AssetImage src={src} label={asset.label} onClick={() => src && setPreviewImage(src)} />
                            {marker ? (
                              <span
                                className="asset-target-marker"
                                style={{ left: `${marker.left}%`, top: `${marker.top}%` }}
                                title={marker.label}
                              >
                                <span>{marker.label}</span>
                              </span>
                            ) : null}
                          </div>
                          <figcaption>
                            <strong>{asset.label}</strong>
                            <span>{new Date(asset.createdAt).toLocaleTimeString()}</span>
                          </figcaption>
                        </figure>
                      );
                    })
                  ) : (
                    <div className="trace-empty">还没有截图素材。</div>
                  )}
                </div>
              </section>
            ) : null}
          </div>
        </>
      )}
    </aside>
  );

  return (
    <>
      {inspector}
      {previewImage ? (
        <div
          className="image-preview-backdrop"
          onClick={() => setPreviewImage(null)}
          onKeyDown={(e) => { if (e.key === "Escape") setPreviewImage(null); }}
          role="button"
          tabIndex={0}
        >
          <button
            className="image-preview-close"
            onClick={() => setPreviewImage(null)}
            type="button"
          >
            <X size={20} />
          </button>
          <img
            className="image-preview-img"
            src={previewImage}
            alt="预览"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      ) : null}
    </>
  );
}
