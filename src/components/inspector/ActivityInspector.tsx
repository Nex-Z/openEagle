import { useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc, invoke } from "../../lib/electron-bridge";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Image,
  ListTree,
  ShieldAlert,
  Wrench,
  X,
} from "lucide-react";
import { executionStateLabel } from "../../lib/runLabels";
import type {
  AgentExecutionTrace,
  SoloConfirmationPayload,
  SoloPlanStatus,
  SoloStatusPayload,
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

export function ActivityInspector(props: ActivityInspectorProps) {
  const {
    traces,
    assets,
    soloStatus,
    soloConfirmation,
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

  const confirmationCount =
    Number(Boolean(soloConfirmation)) + Number(Boolean(toolConfirmation));

  const inspector = (
    <aside
      className={inspectorCollapsed ? "activity-inspector is-collapsed" : "activity-inspector"}
    >
      <header className="inspector-header">
        <div className="inspector-header-copy">
          <p>活动面板</p>
          {!inspectorCollapsed ? <strong>执行侧栏</strong> : null}
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
                <section className="inspector-card">
                  <div className="inspector-card-head">
                    <div>
                      <span className="card-kicker">当前运行状态</span>
                      <strong>{executionStateLabel(soloStatus.state)}</strong>
                    </div>
                  </div>
                </section>

                {soloPlan && <SoloPlanChecklist plan={soloPlan} />}

                <section className="inspector-card">
                  <div className="inspector-card-head">
                    <div>
                      <span className="card-kicker">执行时间线</span>
                      <strong>最近事件</strong>
                    </div>
                    <ListTree size={16} />
                  </div>
                  <div className="timeline-list">
                    {soloTimeline.length > 0 ? (
                      soloTimeline
                        .slice()
                        .reverse()
                        .map((line) => (
                          <div key={line} className="timeline-item">
                            {line}
                          </div>
                        ))
                    ) : (
                      <div className="timeline-empty">暂时没有时间线事件。</div>
                    )}
                  </div>
                </section>

                <section className="inspector-card">
                  <div className="inspector-card-head">
                    <div>
                      <span className="card-kicker">确认与异常</span>
                      <strong>需要你的决定</strong>
                    </div>
                    {confirmationCount > 0 ? <ShieldAlert size={16} /> : <CheckCircle2 size={16} />}
                  </div>

                  {soloConfirmation ? (
                    <div className="decision-card warning">
                      <div className="decision-card-head">
                        <ShieldAlert size={16} />
                        <strong>{soloConfirmation.action}</strong>
                      </div>
                      <p>{soloConfirmation.reason}</p>
                      <small>{soloConfirmation.thoughtSummary}</small>
                      {soloConfirmation.actionArgs ? (
                        <pre>{formatTraceValue(soloConfirmation.actionArgs)}</pre>
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

                  {!soloConfirmation && !toolConfirmation ? (
                    <div className="decision-empty">当前没有待确认动作。</div>
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
                      return (
                        <figure key={asset.id} className="asset-card">
                          <AssetImage src={src} label={asset.label} onClick={() => src && setPreviewImage(src)} />
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
