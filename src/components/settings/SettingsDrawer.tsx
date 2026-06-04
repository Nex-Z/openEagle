import { useEffect, useRef, useState, type CSSProperties } from "react";
import { convertFileSrc, invoke } from "../../lib/electron-bridge";
import { ChevronDown, Feather, Monitor, SlidersHorizontal, Sparkles, Wrench, X } from "lucide-react";
import QRCode from "qrcode";
import { ThemeToggle } from "../ThemeToggle";
import type {
  AppSettings,
  IMStatusPayload,
  MemoryState,
  McpServerConfig,
  ScheduledTask,
  ScheduledTaskExecution,
  SkillConfig,
  SoloDisplayOption,
  ToolConfig,
  WechatBindStatusPayload,
} from "../../types/protocol";
import { SecretInput } from "./SecretInput";

export type SettingsSection =
  | "general"
  | "models"
  | "im"
  | "solo"
  | "tools"
  | "mcp"
  | "skills"
  | "memory"
  | "scheduled_tasks";

interface SettingsDrawerProps {
  open: boolean;
  settings: AppSettings;
  activeSection: SettingsSection;
  soloDisplays: SoloDisplayOption[];
  imStatuses: Partial<Record<IMStatusPayload["provider"], IMStatusPayload>>;
  wechatBindStatus: WechatBindStatusPayload | null;
  scheduledTasks: ScheduledTask[];
  scheduledTaskHistory: Record<string, ScheduledTaskExecution[]>;
  memoryState: MemoryState;
  onRefreshSoloDisplays: () => boolean;
  onRefreshSettings: () => boolean;
  onStartWechatBind: (force?: boolean) => boolean;
  onCancelWechatBind: () => boolean;
  onUnbindWechat: () => boolean;
  onRequestScheduledTasks: () => boolean;
  onRequestMemoryState: () => boolean;
  onSaveMemoryState: (memory: MemoryState) => boolean;
  onCreateScheduledTask: (task: Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">) => boolean;
  onUpdateScheduledTask: (task: ScheduledTask) => boolean;
  onDeleteScheduledTask: (taskId: string) => boolean;
  onRequestScheduledTaskHistory: (taskId: string) => boolean;
  onChange: (settings: AppSettings) => void;
  onClose: () => void;
  onSectionChange: (section: SettingsSection) => void;
}

const sectionMeta: Array<{
  id: SettingsSection;
  title: string;
  summary: string;
}> = [
  { id: "general", title: "General", summary: "外观与基础体验。" },
  { id: "models", title: "Models", summary: "文本模型和视觉模型接入。" },
  { id: "im", title: "IM", summary: "飞书、Telegram 与微信远程接入。" },
  { id: "solo", title: "桌面执行", summary: "显示器预览与截图目标。" },
  { id: "tools", title: "Tools", summary: "本地工具入口。" },
  { id: "mcp", title: "MCP", summary: "MCP Server 配置。" },
  { id: "skills", title: "Skills", summary: "提示技能配置。" },
  { id: "memory", title: "Memory", summary: "用户画像、笔记与 Agent 个性。" },
  { id: "scheduled_tasks", title: "定时任务", summary: "自动执行的任务管理与历史。" },
];

function createToolConfig(): ToolConfig {
  return {
    id: crypto.randomUUID(),
    name: "新工具",
    description: "",
    command: "",
    cwd: "",
    timeoutMs: 30_000,
    tail: 120,
    enabled: true,
  };
}

function createMcpConfig(): McpServerConfig {
  return {
    id: crypto.randomUUID(),
    name: "新 MCP Server",
    transport: "stdio",
    endpoint: "",
    description: "",
    enabled: true,
  };
}

function createSkillConfig(): SkillConfig {
  return {
    id: crypto.randomUUID(),
    name: "新 Skill",
    description: "",
    prompt: "",
    enabled: true,
  };
}

function createMemoryNote() {
  const now = new Date().toISOString();
  return {
    id: crypto.randomUUID(),
    text: "",
    tags: [],
    source: "manual",
    confidence: 1,
    status: "active" as const,
    createdAt: now,
    updatedAt: now,
  };
}

function formatMemoryTime(value?: string) {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function updateListItem<T extends { id: string }>(
  list: T[],
  id: string,
  updater: (item: T) => T,
) {
  return list.map((item) => (item.id === id ? updater(item) : item));
}

function removeListItem<T extends { id: string }>(list: T[], id: string) {
  return list.filter((item) => item.id !== id);
}

function splitLines(value: string) {
  return value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinLines(value: string[]) {
  return value.join("\n");
}

function readBoundedInteger(value: string, fallback: number, min = 0) {
  const next = Number(value);
  if (!Number.isFinite(next)) {
    return fallback;
  }
  return Math.max(min, Math.round(next));
}

function getToolQualityMessages(tool: ToolConfig, tools: ToolConfig[]) {
  const messages: string[] = [];
  const duplicateName =
    tool.name.trim() &&
    tools.some((item) => item.id !== tool.id && item.name.trim() === tool.name.trim());
  const cwd = tool.cwd.trim();
  const looksOutside =
    cwd.startsWith("..") ||
    cwd.includes("../") ||
    cwd.includes("..\\") ||
    /^[A-Za-z]:[\\/]/.test(cwd) ||
    cwd.startsWith("/") ||
    cwd.startsWith("\\");

  if (tool.enabled && !tool.command.trim()) {
    messages.push("启用的工具必须填写固定命令，否则不会注册。");
  }
  if (tool.enabled && !tool.description.trim()) {
    messages.push("建议说明何时使用、会输出什么，帮助 Agent 准确选择。");
  }
  if (duplicateName) {
    messages.push("存在同名工具，建议改成更具体的名称。");
  }
  if (looksOutside) {
    messages.push("cwd 看起来会越出工作区，后端会阻断执行。");
  }
  if (tool.timeoutMs < 1000 || tool.timeoutMs > 120_000) {
    messages.push("timeoutMs 会被限制在 1000 到 120000 之间。");
  }
  if (tool.tail < 1 || tool.tail > 300) {
    messages.push("tail 会被限制在 1 到 300 之间。");
  }
  return messages;
}

function displayPreviewStyle(display: SoloDisplayOption): CSSProperties {
  if (display.width <= 0 || display.height <= 0) {
    return {};
  }
  return {
    aspectRatio: `${display.width} / ${display.height}`,
  };
}

function ConfigListItem(props: {
  title: string;
  subtitle: string;
  enabled: boolean;
  expanded: boolean;
  onToggleEnabled: (value: boolean) => void;
  onToggleExpanded: () => void;
  onDelete: () => void;
  children: React.ReactNode;
}) {
  const {
    title,
    subtitle,
    enabled,
    expanded,
    onToggleEnabled,
    onToggleExpanded,
    onDelete,
    children,
  } = props;

  return (
    <article className="config-row">
      <div className="config-row-head">
        <div className="config-row-copy">
          <strong>{title}</strong>
          <span>{subtitle}</span>
        </div>
        <div className="config-row-actions">
          <label className="toggle-inline">
            <input
              checked={enabled}
              onChange={(event) => onToggleEnabled(event.target.checked)}
              type="checkbox"
            />
            <span className="toggle-track" />
          </label>
          <button className="ghost-button" onClick={onToggleExpanded} type="button">
            {expanded ? "收起" : "编辑"}
          </button>
          <button className="ghost-button danger" onClick={onDelete} type="button">
            删除
          </button>
        </div>
      </div>
      {expanded ? <div className="config-row-body">{children}</div> : null}
    </article>
  );
}

export function SettingsDrawer(props: SettingsDrawerProps) {
  if (!props.open) {
    return null;
  }

  return <SettingsDrawerContent {...props} />;
}

function SettingsDrawerContent(props: SettingsDrawerProps) {
  const {
    open,
    settings,
    activeSection,
    soloDisplays,
    imStatuses,
    wechatBindStatus,
    scheduledTasks,
    scheduledTaskHistory,
    memoryState,
    onRefreshSoloDisplays,
    onRefreshSettings,
    onStartWechatBind,
    onCancelWechatBind,
    onUnbindWechat,
    onRequestScheduledTasks,
    onRequestMemoryState,
    onSaveMemoryState,
    onCreateScheduledTask,
    onUpdateScheduledTask,
    onDeleteScheduledTask,
    onRequestScheduledTaskHistory,
    onChange,
    onClose,
    onSectionChange,
  } = props;
  const [expandedToolId, setExpandedToolId] = useState<string | null>(null);
  const [expandedMcpId, setExpandedMcpId] = useState<string | null>(null);
  const [expandedSkillId, setExpandedSkillId] = useState<string | null>(null);
  const [expandedMemoryNoteId, setExpandedMemoryNoteId] = useState<string | null>(null);
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);
  const [taskFormOpen, setTaskFormOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  const [memoryDraft, setMemoryDraft] = useState<MemoryState>(memoryState);
  const [memoryDirty, setMemoryDirty] = useState(false);
  const [historyTaskId, setHistoryTaskId] = useState<string | null>(null);
  const [collapsedImSections, setCollapsedImSections] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    if (!settings.feishu.enabled) initial.add("feishu");
    if (!settings.telegram.enabled) initial.add("telegram");
    if (!settings.wechat.enabled) initial.add("wechat");
    return initial;
  });
  const [previewDataUrls, setPreviewDataUrls] = useState<Record<string, string>>({});
  const [failedPreviews, setFailedPreviews] = useState<Set<string>>(new Set());
  const [wechatQrDataUrl, setWechatQrDataUrl] = useState<string | null>(null);
  const attemptedPathsRef = useRef<Set<string>>(new Set());
  const prevSoloDisplaysRef = useRef<SoloDisplayOption[]>([]);

  useEffect(() => {
    if (open && activeSection === "solo") {
      onRefreshSoloDisplays();
    }
    if (open && activeSection === "scheduled_tasks") {
      onRequestScheduledTasks();
    }
    if (open && activeSection === "memory") {
      onRequestMemoryState();
    }
  }, [
    activeSection,
    onRefreshSoloDisplays,
    onRequestMemoryState,
    onRequestScheduledTasks,
    open,
  ]);

  useEffect(() => {
    if (!memoryDirty) {
      setMemoryDraft(memoryState);
    }
  }, [memoryDirty, memoryState]);

  useEffect(() => {
    const displaysChanged =
      soloDisplays.length !== prevSoloDisplaysRef.current.length ||
      soloDisplays.some(
        (d, i) => d.previewPath !== prevSoloDisplaysRef.current[i]?.previewPath,
      );
    if (displaysChanged) {
      prevSoloDisplaysRef.current = soloDisplays;
      attemptedPathsRef.current = new Set();
      setPreviewDataUrls({});
      setFailedPreviews(new Set());
    }

    const previewPaths = soloDisplays
      .map((display) => display.previewPath)
      .filter(Boolean) as string[];
    const missing = previewPaths.filter(
      (path) => !previewDataUrls[path] && !attemptedPathsRef.current.has(path),
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
          console.warn("read_image_data_url failed:", path, err);
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
      setPreviewDataUrls((current) => {
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
  }, [soloDisplays]);

  const wechatIsBinding =
    wechatBindStatus?.state === "qrcode" || wechatBindStatus?.state === "waiting";
  const wechatQrContent = wechatIsBinding ? wechatBindStatus?.qrcodeUrl ?? "" : "";

  useEffect(() => {
    if (!wechatQrContent) {
      setWechatQrDataUrl(null);
      return;
    }

    let cancelled = false;
    if (wechatQrContent.startsWith("data:image/")) {
      setWechatQrDataUrl(wechatQrContent);
      return;
    }

    QRCode.toDataURL(wechatQrContent, {
      margin: 1,
      width: 196,
    })
      .then((dataUrl) => {
        if (!cancelled) {
          setWechatQrDataUrl(dataUrl);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setWechatQrDataUrl(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [wechatQrContent]);

  const updateMemoryDraft = (updater: (memory: MemoryState) => MemoryState) => {
    setMemoryDirty(true);
    setMemoryDraft((current) => updater(current));
  };

  const saveMemoryDraft = () => {
    if (onSaveMemoryState(memoryDraft)) {
      setMemoryDirty(false);
    }
  };

  const visibleMemoryNotes = memoryDraft.notes.filter(
    (note) => note.status !== "archived",
  );

  const updateContextSettings = (patch: Partial<AppSettings["context"]>) => {
    onChange({
      ...settings,
      context: {
        ...settings.context,
        ...patch,
      },
    });
  };

  const activeMeta = sectionMeta.find((section) => section.id === activeSection) ?? sectionMeta[0];
  const feishuStatus = imStatuses.feishu;
  const telegramStatus = imStatuses.telegram;
  const wechatStatus = imStatuses.wechat;

  return (
    <>
      <div className={open ? "settings-backdrop is-visible" : "settings-backdrop"} onClick={onClose} />
      <aside className="settings-drawer is-open motion-safe:animate-[eagle-drawer-in_220ms_ease-out_both]">
        <div className="settings-drawer-nav">
          <div className="settings-drawer-brand">
            <div className="brand-emblem small" aria-hidden="true">
              <Feather size={17} />
            </div>
            <div>
              <strong>Settings</strong>
              <span>即时保存</span>
            </div>
          </div>

          <div className="settings-nav-list">
            {sectionMeta.map((section) => (
              <button
                key={section.id}
                className={section.id === activeSection ? "settings-nav-item is-active" : "settings-nav-item"}
                onClick={() => onSectionChange(section.id)}
                type="button"
              >
                <span>{section.title}</span>
                <small>{section.summary}</small>
              </button>
            ))}
          </div>
        </div>

        <div className="settings-drawer-content">
          <header className="settings-drawer-header">
            <div>
              <p>{activeMeta.title}</p>
              <h2>{activeMeta.summary}</h2>
            </div>
            <button className="icon-button" onClick={onClose} type="button">
              <X size={16} />
            </button>
          </header>

          <div className="settings-content-scroll">
            {activeSection === "general" ? (
              <div className="settings-stack two-column">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">界面</span>
                      <strong>主题模式</strong>
                    </div>
                    <SlidersHorizontal size={16} />
                  </div>
                  <ThemeToggle
                    onChange={(themeMode) =>
                      onChange({
                        ...settings,
                        appearance: { themeMode },
                      })
                    }
                    value={settings.appearance.themeMode}
                  />
                </section>

                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Context</span>
                      <strong>上下文整理</strong>
                    </div>
                    <Wrench size={16} />
                  </div>
                  <label className="form-switch">
                    <span>启用整理</span>
                    <div className="toggle-switch">
                      <input
                        checked={settings.context.enabled}
                        onChange={(event) =>
                          updateContextSettings({ enabled: event.target.checked })
                        }
                        type="checkbox"
                      />
                      <span className="toggle-track" />
                    </div>
                  </label>
                  <label className="form-field">
                    <span>触发 token 阈值</span>
                    <input
                      min={1000}
                      onChange={(event) =>
                        updateContextSettings({
                          maxInputTokens: readBoundedInteger(
                            event.target.value,
                            settings.context.maxInputTokens,
                            1000,
                          ),
                        })
                      }
                      step={1000}
                      type="number"
                      value={settings.context.maxInputTokens}
                    />
                  </label>
                  <label className="form-field">
                    <span>保留最近消息数</span>
                    <input
                      min={0}
                      onChange={(event) =>
                        updateContextSettings({
                          preserveRecentMessages: readBoundedInteger(
                            event.target.value,
                            settings.context.preserveRecentMessages,
                          ),
                        })
                      }
                      type="number"
                      value={settings.context.preserveRecentMessages}
                    />
                  </label>
                  <label className="form-field">
                    <span>IM 静默整理分钟</span>
                    <input
                      min={0}
                      onChange={(event) =>
                        updateContextSettings({
                          imIdleCleanupMinutes: readBoundedInteger(
                            event.target.value,
                            settings.context.imIdleCleanupMinutes,
                          ),
                        })
                      }
                      type="number"
                      value={settings.context.imIdleCleanupMinutes}
                    />
                  </label>
                  <label className="form-field">
                    <span>工具消息处理</span>
                    <select
                      onChange={(event) =>
                        updateContextSettings({
                          toolMessageMode: event.target.value as AppSettings["context"]["toolMessageMode"],
                        })
                      }
                      value={settings.context.toolMessageMode}
                    >
                      <option value="placeholder">占位</option>
                      <option value="remove">移除</option>
                    </select>
                  </label>
                  <label className="form-switch">
                    <span>AI 摘要中段</span>
                    <div className="toggle-switch">
                      <input
                        checked={settings.context.aiSummaryEnabled}
                        onChange={(event) =>
                          updateContextSettings({ aiSummaryEnabled: event.target.checked })
                        }
                        type="checkbox"
                      />
                      <span className="toggle-track" />
                    </div>
                  </label>
                  <label className="form-switch">
                    <span>压缩前写入记忆快照</span>
                    <div className="toggle-switch">
                      <input
                        checked={settings.context.snapshotOnCompaction}
                        onChange={(event) =>
                          updateContextSettings({ snapshotOnCompaction: event.target.checked })
                        }
                        type="checkbox"
                      />
                      <span className="toggle-track" />
                    </div>
                  </label>
                  <label className="form-field">
                    <span>摘要字符上限</span>
                    <input
                      min={200}
                      onChange={(event) =>
                        updateContextSettings({
                          summaryCharLimit: readBoundedInteger(
                            event.target.value,
                            settings.context.summaryCharLimit,
                            200,
                          ),
                        })
                      }
                      type="number"
                      value={settings.context.summaryCharLimit}
                    />
                  </label>
                  <label className="form-field">
                    <span>工具结果字符上限</span>
                    <input
                      min={0}
                      onChange={(event) =>
                        updateContextSettings({
                          toolResultCharLimit: readBoundedInteger(
                            event.target.value,
                            settings.context.toolResultCharLimit,
                          ),
                        })
                      }
                      type="number"
                      value={settings.context.toolResultCharLimit}
                    />
                  </label>
                  <label className="form-field">
                    <span>中段消息字符上限</span>
                    <input
                      min={0}
                      onChange={(event) =>
                        updateContextSettings({
                          middleMessageCharLimit: readBoundedInteger(
                            event.target.value,
                            settings.context.middleMessageCharLimit,
                          ),
                        })
                      }
                      type="number"
                      value={settings.context.middleMessageCharLimit}
                    />
                  </label>
                </section>
              </div>
            ) : null}

            {activeSection === "im" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">状态</span>
                      <strong>IM 连接</strong>
                    </div>
                  </div>
                  <div className="form-hint">
                    <span>飞书: {feishuStatus?.state ?? "disabled"} · {feishuStatus?.detail || "未启动"}</span>
                    {feishuStatus?.lastBlockedOpenId ? (
                      <span>飞书最近拦截 open_id: {feishuStatus.lastBlockedOpenId}</span>
                    ) : null}
                    {feishuStatus?.lastBlockedChatId ? (
                      <span>飞书最近拦截 chat_id: {feishuStatus.lastBlockedChatId}</span>
                    ) : null}
                    <span>Telegram: {telegramStatus?.state ?? "disabled"} · {telegramStatus?.detail || "未启动"}</span>
                    {telegramStatus?.lastBlockedOpenId ? (
                      <span>Telegram 最近拦截 user_id: {telegramStatus.lastBlockedOpenId}</span>
                    ) : null}
                    {telegramStatus?.lastBlockedChatId ? (
                      <span>Telegram 最近拦截 chat_id: {telegramStatus.lastBlockedChatId}</span>
                    ) : null}
                    <span>微信: {wechatStatus?.state ?? "disabled"} · {wechatStatus?.detail || "未启动"}</span>
                    {wechatStatus?.lastBlockedOpenId ? (
                      <span>微信最近拦截 user_id: {wechatStatus.lastBlockedOpenId}</span>
                    ) : null}
                    {wechatStatus?.lastBlockedChatId ? (
                      <span>微信最近拦截 chat_id: {wechatStatus.lastBlockedChatId}</span>
                    ) : null}
                  </div>
                </section>

                <section className="settings-panel">
                  <div
                    className="settings-panel-head settings-panel-collapsible"
                    onClick={() =>
                      setCollapsedImSections((prev) => {
                        const next = new Set(prev);
                        if (next.has("feishu")) next.delete("feishu");
                        else next.add("feishu");
                        return next;
                      })
                    }
                  >
                    <div>
                      <span className="card-kicker">飞书</span>
                      <strong>长连接机器人</strong>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <label
                        className="toggle-inline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          checked={settings.feishu.enabled}
                          onChange={(event) =>
                            onChange({
                              ...settings,
                              feishu: {
                                ...settings.feishu,
                                enabled: event.target.checked,
                              },
                            })
                          }
                          type="checkbox"
                        />
                        <span className="toggle-track" />
                      </label>
                      <ChevronDown
                        size={14}
                        style={{
                          transform: collapsedImSections.has("feishu") ? "none" : "rotate(180deg)",
                          transition: "transform 0.15s",
                          color: "rgba(87, 99, 95, 0.4)",
                        }}
                      />
                    </div>
                  </div>
                  {!collapsedImSections.has("feishu") ? (
                  <>
                  <label className="form-field">
                    <span>App ID</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          feishu: {
                            ...settings.feishu,
                            appId: event.target.value,
                          },
                        })
                      }
                      value={settings.feishu.appId}
                    />
                  </label>
                  <label className="form-field">
                    <span>App Secret</span>
                    <SecretInput
                      onChange={(value) =>
                        onChange({
                          ...settings,
                          feishu: {
                            ...settings.feishu,
                            appSecret: value,
                          },
                        })
                      }
                      value={settings.feishu.appSecret}
                    />
                  </label>
                  <div className="form-hint">
                    <span>open_id 用于授权单个飞书用户，chat_id 用于授权一个私聊或群聊。</span>
                    <span>两者任意一个命中即可放行；都为空时会拦截所有飞书消息。</span>
                    <span>首次配置可先给机器人发一条消息，再从下方“最近拦截”的 open_id / chat_id 复制到这里。</span>
                  </div>
                  <label className="form-field">
                    <span>允许的 open_id</span>
                    <textarea
                      className="form-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          feishu: {
                            ...settings.feishu,
                            allowedOpenIds: splitLines(event.target.value),
                          },
                        })
                      }
                      placeholder="每行一个 open_id"
                      value={joinLines(settings.feishu.allowedOpenIds)}
                    />
                  </label>
                  <label className="form-field">
                    <span>允许的 chat_id</span>
                    <textarea
                      className="form-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          feishu: {
                            ...settings.feishu,
                            allowedChatIds: splitLines(event.target.value),
                          },
                        })
                      }
                      placeholder="每行一个 chat_id"
                      value={joinLines(settings.feishu.allowedChatIds)}
                    />
                  </label>
                  </>
                  ) : null}
                </section>

                <section className="settings-panel">
                  <div
                    className="settings-panel-head settings-panel-collapsible"
                    onClick={() =>
                      setCollapsedImSections((prev) => {
                        const next = new Set(prev);
                        if (next.has("telegram")) next.delete("telegram");
                        else next.add("telegram");
                        return next;
                      })
                    }
                  >
                    <div>
                      <span className="card-kicker">Telegram</span>
                      <strong>Bot 长轮询</strong>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <label
                        className="toggle-inline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          checked={settings.telegram.enabled}
                          onChange={(event) =>
                            onChange({
                              ...settings,
                              telegram: {
                                ...settings.telegram,
                                enabled: event.target.checked,
                              },
                            })
                          }
                          type="checkbox"
                        />
                        <span className="toggle-track" />
                      </label>
                      <ChevronDown
                        size={14}
                        style={{
                          transform: collapsedImSections.has("telegram") ? "none" : "rotate(180deg)",
                          transition: "transform 0.15s",
                          color: "rgba(87, 99, 95, 0.4)",
                        }}
                      />
                    </div>
                  </div>
                  {!collapsedImSections.has("telegram") ? (
                  <>
                  <label className="form-field">
                    <span>Bot Token</span>
                    <SecretInput
                      onChange={(value) =>
                        onChange({
                          ...settings,
                          telegram: {
                            ...settings.telegram,
                            botToken: value,
                          },
                        })
                      }
                      value={settings.telegram.botToken}
                    />
                  </label>
                  <label className="form-field">
                    <span>允许的 user_id</span>
                    <textarea
                      className="form-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          telegram: {
                            ...settings.telegram,
                            allowedUserIds: splitLines(event.target.value),
                          },
                        })
                      }
                      placeholder="每行一个 Telegram user_id"
                      value={joinLines(settings.telegram.allowedUserIds)}
                    />
                  </label>
                  <label className="form-field">
                    <span>允许的 chat_id</span>
                    <textarea
                      className="form-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          telegram: {
                            ...settings.telegram,
                            allowedChatIds: splitLines(event.target.value),
                          },
                        })
                      }
                      placeholder="每行一个 Telegram chat_id"
                      value={joinLines(settings.telegram.allowedChatIds)}
                    />
                  </label>
                  </>
                  ) : null}
                </section>

                <section className="settings-panel">
                  <div
                    className="settings-panel-head settings-panel-collapsible"
                    onClick={() =>
                      setCollapsedImSections((prev) => {
                        const next = new Set(prev);
                        if (next.has("wechat")) next.delete("wechat");
                        else next.add("wechat");
                        return next;
                      })
                    }
                  >
                    <div>
                      <span className="card-kicker">微信</span>
                      <strong>ClawBot 扫码接入</strong>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <label
                        className="toggle-inline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          checked={settings.wechat.enabled}
                          onChange={(event) =>
                            onChange({
                              ...settings,
                              wechat: {
                                ...settings.wechat,
                                enabled: event.target.checked,
                              },
                            })
                          }
                          type="checkbox"
                        />
                        <span className="toggle-track" />
                      </label>
                      <ChevronDown
                        size={14}
                        style={{
                          transform: collapsedImSections.has("wechat") ? "none" : "rotate(180deg)",
                          transition: "transform 0.15s",
                          color: "rgba(87, 99, 95, 0.4)",
                        }}
                      />
                    </div>
                  </div>
                  {!collapsedImSections.has("wechat") ? (
                  <>
                  <div className="form-hint">
                    <span>绑定状态: {wechatBindStatus?.message || settings.wechat.accountId || "未绑定"}</span>
                    {settings.wechat.accountId ? (
                      <span>Account ID: {settings.wechat.accountId}</span>
                    ) : null}
                  </div>
                  <div className="inline-actions">
                    <button
                      className="ghost-button"
                      onClick={() => onStartWechatBind(Boolean(settings.wechat.accountId))}
                      type="button"
                    >
                      {settings.wechat.accountId ? "重新扫码" : "扫码绑定"}
                    </button>
                    {wechatIsBinding ? (
                      <button className="ghost-button" onClick={onCancelWechatBind} type="button">
                        取消绑定
                      </button>
                    ) : null}
                    <button
                      className="ghost-button danger"
                      disabled={!settings.wechat.accountId && !wechatIsBinding}
                      onClick={onUnbindWechat}
                      type="button"
                    >
                      解绑
                    </button>
                  </div>
                  {wechatQrDataUrl ? (
                    <div className="wechat-qr-panel">
                      <img alt="微信扫码绑定二维码" src={wechatQrDataUrl} />
                    </div>
                  ) : null}
                  </>
                  ) : null}
                </section>

              </div>
            ) : null}

            {activeSection === "models" ? (
              <div className="settings-stack two-column">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">文本模型</span>
                      <strong>主 Agent 推理</strong>
                    </div>
                    <Wrench size={16} />
                  </div>
                  <label className="form-field">
                    <span>Provider</span>
                    <select
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            provider: event.target.value as AppSettings["agent"]["provider"],
                          },
                        })
                      }
                      value={settings.agent.provider}
                    >
                      <option value="mock">mock</option>
                      <option value="openai">openai</option>
                      <option value="openai-like">openai-like</option>
                      <option value="anthropic">anthropic</option>
                    </select>
                  </label>
                  <label className="form-field">
                    <span>模型 ID</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            modelId: event.target.value,
                          },
                        })
                      }
                      value={settings.agent.modelId}
                    />
                  </label>
                  <label className="form-field">
                    <span>API Key</span>
                    <SecretInput
                      onChange={(value) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            apiKey: value,
                          },
                        })
                      }
                      value={settings.agent.apiKey}
                    />
                  </label>
                  <label className="form-field">
                    <span>Base URL</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            baseUrl: event.target.value,
                          },
                        })
                      }
                      value={settings.agent.baseUrl}
                    />
                  </label>
                </section>

                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">视觉模型</span>
                      <strong>视觉执行</strong>
                    </div>
                    <Monitor size={16} />
                  </div>
                  <label className="form-field">
                    <span>Provider</span>
                    <select
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            vlProvider: event.target.value as AppSettings["agent"]["vlProvider"],
                          },
                        })
                      }
                      value={settings.agent.vlProvider}
                    >
                      <option value="openai">openai</option>
                      <option value="openai-like">openai-like</option>
                      <option value="anthropic">anthropic</option>
                    </select>
                  </label>
                  <label className="form-field">
                    <span>模型 ID</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            vlModelId: event.target.value,
                          },
                        })
                      }
                      value={settings.agent.vlModelId}
                    />
                  </label>
                  <label className="form-field">
                    <span>API Key</span>
                    <SecretInput
                      onChange={(value) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            vlApiKey: value,
                          },
                        })
                      }
                      value={settings.agent.vlApiKey}
                    />
                  </label>
                  <label className="form-field">
                    <span>Base URL</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          agent: {
                            ...settings.agent,
                            vlBaseUrl: event.target.value,
                          },
                        })
                      }
                      value={settings.agent.vlBaseUrl}
                    />
                  </label>
                </section>
              </div>
            ) : null}

            {activeSection === "solo" ? (
              <div className="settings-stack two-column">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">显示器预览</span>
                      <strong>截图目标</strong>
                    </div>
                    <button className="ghost-button" onClick={onRefreshSoloDisplays} type="button">
                      刷新
                    </button>
                  </div>
                  <div className="display-grid">
                    {soloDisplays.length > 0 ? (
                      soloDisplays.map((display) => {
                        const isActive = settings.solo.preferredDisplayIndex === display.index;
                        const previewStyle = displayPreviewStyle(display);
                        return (
                          <label
                            key={display.index}
                            className={isActive ? "display-card is-active" : "display-card"}
                          >
                            <input
                              checked={isActive}
                              name="solo-display"
                              onChange={() =>
                                onChange({
                                  ...settings,
                                  solo: {
                                    ...settings.solo,
                                    preferredDisplayIndex: display.index,
                                  },
                                })
                              }
                              type="radio"
                            />
                            <div className="display-card-copy">
                              <strong>{display.label}</strong>
                              <span>
                                {display.width}x{display.height} · ({display.left}, {display.top})
                              </span>
                            </div>
                            {display.previewPath && !failedPreviews.has(display.previewPath) ? (
                              <img
                                alt={display.label}
                                src={
                                  previewDataUrls[display.previewPath] ||
                                  (display.previewPath.startsWith("data:")
                                    ? display.previewPath
                                    : convertFileSrc(display.previewPath))
                                }
                                style={previewStyle}
                                onError={() => {
                                  console.warn("[solo] display preview load failed:", display.previewPath);
                                  setFailedPreviews((prev) => new Set(prev).add(display.previewPath!));
                                }}
                              />
                            ) : (
                              <div className="display-preview-empty" style={previewStyle}>
                                无预览图
                              </div>
                            )}
                          </label>
                        );
                      })
                    ) : (
                      <div className="display-preview-empty">暂无显示器预览。</div>
                    )}
                  </div>
                </section>
              </div>
            ) : null}

            {activeSection === "tools" ? (
              <div className="settings-stack">
                <div className="settings-section-head">
                  <div>
                    <span className="card-kicker">执行入口</span>
                    <strong>工具列表</strong>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button
                      className="ghost-button"
                      onClick={() => onRefreshSettings()}
                      type="button"
                    >
                      刷新
                    </button>
                    <button
                      className="ghost-button"
                      onClick={() =>
                        onChange({
                          ...settings,
                          tools: [...settings.tools, createToolConfig()],
                        })
                      }
                      type="button"
                    >
                      新增工具
                    </button>
                  </div>
                </div>
                <div className="config-list">
                    {settings.tools.map((tool) => {
                      const qualityMessages = getToolQualityMessages(tool, settings.tools);
                      return (
                      <ConfigListItem
                        key={tool.id}
                        enabled={tool.enabled}
                        expanded={expandedToolId === tool.id}
                        onDelete={() =>
                          onChange({
                            ...settings,
                            tools: removeListItem(settings.tools, tool.id),
                          })
                        }
                        onToggleEnabled={(value) =>
                          onChange({
                            ...settings,
                            tools: updateListItem(settings.tools, tool.id, (item) => ({
                              ...item,
                              enabled: value,
                            })),
                          })
                        }
                        onToggleExpanded={() =>
                          setExpandedToolId((current) => (current === tool.id ? null : tool.id))
                        }
                        subtitle={tool.command || "未配置命令"}
                        title={tool.name || "未命名工具"}
                      >
                        {qualityMessages.length > 0 ? (
                          <div className="form-hint warning">
                            {qualityMessages.map((message) => (
                              <span key={message}>{message}</span>
                            ))}
                          </div>
                        ) : null}
                        <label className="form-field">
                          <span>名称</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  name: event.target.value,
                                })),
                              })
                            }
                            value={tool.name}
                          />
                        </label>
                        <label className="form-field">
                          <span>命令</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  command: event.target.value,
                                })),
                              })
                            }
                            value={tool.command}
                          />
                        </label>
                        <label className="form-field">
                          <span>工作目录</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  cwd: event.target.value,
                                })),
                              })
                            }
                            placeholder="留空表示工作区根目录"
                            value={tool.cwd}
                          />
                        </label>
                        <label className="form-field">
                          <span>超时 (ms)</span>
                          <input
                            min={1000}
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  timeoutMs: Number(event.target.value) || 30_000,
                                })),
                              })
                            }
                            type="number"
                            value={tool.timeoutMs}
                          />
                        </label>
                        <label className="form-field">
                          <span>输出尾部行数</span>
                          <input
                            min={1}
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  tail: Number(event.target.value) || 120,
                                })),
                              })
                            }
                            type="number"
                            value={tool.tail}
                          />
                        </label>
                        <label className="form-field">
                          <span>说明</span>
                          <textarea
                            className="form-textarea"
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                tools: updateListItem(settings.tools, tool.id, (item) => ({
                                  ...item,
                                  description: event.target.value,
                                })),
                              })
                            }
                            placeholder="说明何时使用、固定命令会输出什么、适合哪些任务。"
                            value={tool.description}
                          />
                        </label>
                      </ConfigListItem>
                      );
                    })}
                </div>

                <div className="settings-section-head">
                  <div>
                    <span className="card-kicker">Built-in</span>
                    <strong>内置工具</strong>
                  </div>
                </div>
                <div className="config-list">
                    {settings.builtinTools.map((bt) => (
                      <article key={bt.id} className="config-row">
                        <div className="config-row-head">
                          <div className="config-row-copy">
                            <strong>{bt.name}</strong>
                            <span>{bt.description}</span>
                          </div>
                          <div className="config-row-actions">
                            <label className="toggle-inline">
                              <input
                                type="checkbox"
                                checked={bt.enabled}
                                onChange={(e) =>
                                  onChange({
                                    ...settings,
                                    builtinTools: settings.builtinTools.map((item) =>
                                      item.id === bt.id ? { ...item, enabled: e.target.checked } : item,
                                    ),
                                  })
                                }
                              />
                              <span className="toggle-track" />
                            </label>
                          </div>
                        </div>
                      </article>
                    ))}
                </div>
              </div>
            ) : null}

            {activeSection === "mcp" ? (
              <div className="settings-stack">
                <div className="settings-section-head">
                  <div>
                    <span className="card-kicker">Model Context Protocol</span>
                    <strong>MCP Server</strong>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button
                      className="ghost-button"
                      onClick={() => onRefreshSettings()}
                      type="button"
                    >
                      刷新
                    </button>
                    <button
                      className="ghost-button"
                      onClick={() =>
                        onChange({
                          ...settings,
                          mcp: [...settings.mcp, createMcpConfig()],
                        })
                      }
                      type="button"
                    >
                      新增 MCP
                    </button>
                  </div>
                </div>
                <div className="config-list">
                    {settings.mcp.map((server) => (
                      <ConfigListItem
                        key={server.id}
                        enabled={server.enabled}
                        expanded={expandedMcpId === server.id}
                        onDelete={() =>
                          onChange({
                            ...settings,
                            mcp: removeListItem(settings.mcp, server.id),
                          })
                        }
                        onToggleEnabled={(value) =>
                          onChange({
                            ...settings,
                            mcp: updateListItem(settings.mcp, server.id, (item) => ({
                              ...item,
                              enabled: value,
                            })),
                          })
                        }
                        onToggleExpanded={() =>
                          setExpandedMcpId((current) => (current === server.id ? null : server.id))
                        }
                        subtitle={`${server.transport} · ${server.endpoint || "未配置端点"}`}
                        title={server.name || "未命名 MCP"}
                      >
                        <label className="form-field">
                          <span>名称</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                mcp: updateListItem(settings.mcp, server.id, (item) => ({
                                  ...item,
                                  name: event.target.value,
                                })),
                              })
                            }
                            value={server.name}
                          />
                        </label>
                        <label className="form-field">
                          <span>Transport</span>
                          <select
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                mcp: updateListItem(settings.mcp, server.id, (item) => ({
                                  ...item,
                                  transport: event.target.value as McpServerConfig["transport"],
                                })),
                              })
                            }
                            value={server.transport}
                          >
                            <option value="stdio">stdio</option>
                            <option value="http">http</option>
                            <option value="streamable-http">streamable-http</option>
                            <option value="sse">sse</option>
                          </select>
                        </label>
                        <label className="form-field">
                          <span>{server.transport === "stdio" ? "启动命令" : "端点 URL"}</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                mcp: updateListItem(settings.mcp, server.id, (item) => ({
                                  ...item,
                                  endpoint: event.target.value,
                                })),
                              })
                            }
                            placeholder={
                              server.transport === "stdio"
                                ? "例如 npx @modelcontextprotocol/server-filesystem ."
                                : "例如 http://localhost:3001/mcp"
                            }
                            value={server.endpoint}
                          />
                        </label>
                        <label className="form-field">
                          <span>说明</span>
                          <textarea
                            className="form-textarea"
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                mcp: updateListItem(settings.mcp, server.id, (item) => ({
                                  ...item,
                                  description: event.target.value,
                                })),
                              })
                            }
                            value={server.description}
                          />
                        </label>
                      </ConfigListItem>
                    ))}
                </div>
              </div>
            ) : null}

            {activeSection === "skills" ? (
              <div className="settings-stack">
                <div className="settings-section-head">
                  <div>
                    <span className="card-kicker">Prompt Skills</span>
                    <strong>Skill 列表</strong>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button
                      className="ghost-button"
                      onClick={() => onRefreshSettings()}
                      type="button"
                    >
                      刷新
                    </button>
                    <button
                      className="ghost-button"
                      onClick={() =>
                        onChange({
                          ...settings,
                          skills: [...settings.skills, createSkillConfig()],
                        })
                      }
                      type="button"
                    >
                      新增 Skill
                    </button>
                  </div>
                </div>
                <div className="config-list">
                  {settings.skills.map((skill) => (
                    <ConfigListItem
                      key={skill.id}
                      enabled={skill.enabled}
                      expanded={expandedSkillId === skill.id}
                      onDelete={() =>
                        onChange({
                          ...settings,
                          skills: removeListItem(settings.skills, skill.id),
                        })
                      }
                      onToggleEnabled={(value) =>
                          onChange({
                            ...settings,
                            skills: updateListItem(settings.skills, skill.id, (item) => ({
                              ...item,
                              enabled: value,
                            })),
                          })
                        }
                        onToggleExpanded={() =>
                          setExpandedSkillId((current) =>
                            current === skill.id ? null : skill.id,
                          )
                        }
                        subtitle={skill.description || "未填写说明"}
                        title={skill.name || "未命名 Skill"}
                      >
                        <label className="form-field">
                          <span>名称</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                skills: updateListItem(settings.skills, skill.id, (item) => ({
                                  ...item,
                                  name: event.target.value,
                                })),
                              })
                            }
                            value={skill.name}
                          />
                        </label>
                        <label className="form-field">
                          <span>说明</span>
                          <input
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                skills: updateListItem(settings.skills, skill.id, (item) => ({
                                  ...item,
                                  description: event.target.value,
                                })),
                              })
                            }
                            value={skill.description}
                          />
                        </label>
                        <label className="form-field">
                          <span>提示词</span>
                          <textarea
                            className="form-textarea lg"
                            onChange={(event) =>
                              onChange({
                                ...settings,
                                skills: updateListItem(settings.skills, skill.id, (item) => ({
                                  ...item,
                                  prompt: event.target.value,
                                })),
                              })
                            }
                            value={skill.prompt}
                          />
                        </label>
                      </ConfigListItem>
                    ))}
                </div>
              </div>
            ) : null}

            {activeSection === "memory" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Memory</span>
                      <strong>长期记忆</strong>
                    </div>
                    <div className="config-row-actions">
                      <button className="ghost-button" onClick={onRequestMemoryState} type="button">
                        刷新
                      </button>
                      <button
                        className="ghost-button"
                        disabled={!memoryDirty}
                        onClick={saveMemoryDraft}
                        type="button"
                      >
                        保存记忆
                      </button>
                    </div>
                  </div>
                  <label className="form-field">
                    <span>用户画像</span>
                    <textarea
                      className="form-textarea lg"
                      onChange={(event) =>
                        updateMemoryDraft((memory) => ({
                          ...memory,
                          profile: {
                            ...memory.profile,
                            content: event.target.value,
                            updatedAt: new Date().toISOString(),
                          },
                        }))
                      }
                      placeholder="长期稳定的用户信息、工作方式、偏好和背景。"
                      value={memoryDraft.profile.content}
                    />
                  </label>
                  <label className="form-field">
                    <span>Soul</span>
                    <textarea
                      className="form-textarea lg"
                      onChange={(event) =>
                        updateMemoryDraft((memory) => ({
                          ...memory,
                          agentSoul: {
                            ...memory.agentSoul,
                            core: event.target.value,
                            updatedAt: new Date().toISOString(),
                          },
                        }))
                      }
                      placeholder="默认 SOUL.md，可由用户手动维护。"
                      value={memoryDraft.agentSoul.core}
                    />
                  </label>
                  <label className="form-field">
                    <span>Agent 旁注</span>
                    <textarea
                      className="form-textarea"
                      onChange={(event) =>
                        updateMemoryDraft((memory) => ({
                          ...memory,
                          agentSoul: {
                            ...memory.agentSoul,
                            sideNotes: event.target.value,
                            sideNotesUpdatedAt: new Date().toISOString(),
                            updatedAt: new Date().toISOString(),
                          },
                        }))
                      }
                      placeholder="相处方式、称呼、语气和表达习惯，可由 Agent 或用户维护。"
                      value={memoryDraft.agentSoul.sideNotes}
                    />
                  </label>
                </section>

                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Notes</span>
                      <strong>用户笔记</strong>
                    </div>
                    <button
                      className="ghost-button"
                      onClick={() =>
                        updateMemoryDraft((memory) => ({
                          ...memory,
                          notes: [createMemoryNote(), ...memory.notes],
                        }))
                      }
                      type="button"
                    >
                      新增笔记
                    </button>
                  </div>
                  <div className="config-list">
                    {visibleMemoryNotes.length === 0 ? (
                      <div className="form-hint">暂无用户笔记。</div>
                    ) : null}
                    {visibleMemoryNotes.map((note) => (
                      <ConfigListItem
                        key={note.id}
                        enabled={note.status === "active"}
                        expanded={expandedMemoryNoteId === note.id}
                        onDelete={() => {
                          setExpandedMemoryNoteId((current) =>
                            current === note.id ? null : current,
                          );
                          updateMemoryDraft((memory) => ({
                            ...memory,
                            notes: updateListItem(memory.notes, note.id, (item) => ({
                              ...item,
                              status: "archived",
                              updatedAt: new Date().toISOString(),
                            })),
                          }));
                        }}
                        onToggleEnabled={(value) =>
                          updateMemoryDraft((memory) => ({
                            ...memory,
                            notes: updateListItem(memory.notes, note.id, (item) => ({
                              ...item,
                              status: value ? "active" : "archived",
                              updatedAt: new Date().toISOString(),
                            })),
                          }))
                        }
                        onToggleExpanded={() =>
                          setExpandedMemoryNoteId((current) =>
                            current === note.id ? null : note.id,
                          )
                        }
                        subtitle={`${note.status === "active" ? "活跃" : "已归档"} · ${note.source || "manual"} · ${formatMemoryTime(note.updatedAt)}`}
                        title={note.text || "空笔记"}
                      >
                        <label className="form-field">
                          <span>内容</span>
                          <textarea
                            className="form-textarea"
                            onChange={(event) =>
                              updateMemoryDraft((memory) => ({
                                ...memory,
                                notes: updateListItem(memory.notes, note.id, (item) => ({
                                  ...item,
                                  text: event.target.value,
                                  updatedAt: new Date().toISOString(),
                                })),
                              }))
                            }
                            value={note.text}
                          />
                        </label>
                        <label className="form-field">
                          <span>标签（逗号分隔）</span>
                          <input
                            onChange={(event) =>
                              updateMemoryDraft((memory) => ({
                                ...memory,
                                notes: updateListItem(memory.notes, note.id, (item) => ({
                                  ...item,
                                  tags: event.target.value
                                    .split(",")
                                    .map((tag) => tag.trim())
                                    .filter(Boolean),
                                  updatedAt: new Date().toISOString(),
                                })),
                              }))
                            }
                            value={note.tags.join(", ")}
                          />
                        </label>
                        <label className="form-field">
                          <span>置信度</span>
                          <input
                            max={1}
                            min={0}
                            onChange={(event) =>
                              updateMemoryDraft((memory) => ({
                                ...memory,
                                notes: updateListItem(memory.notes, note.id, (item) => ({
                                  ...item,
                                  confidence: Number(event.target.value),
                                  updatedAt: new Date().toISOString(),
                                })),
                              }))
                            }
                            step={0.05}
                            type="number"
                            value={note.confidence}
                          />
                        </label>
                      </ConfigListItem>
                    ))}
                  </div>
                </section>

                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Audit</span>
                      <strong>审计与原始事件</strong>
                    </div>
                  </div>
                  <div className="config-list">
                    {memoryDraft.audit.slice(0, 12).map((item) => (
                      <article className="config-row" key={item.id}>
                        <div className="config-row-head">
                          <div className="config-row-copy">
                            <strong>{item.action} · {item.targetKind}</strong>
                            <span>{item.summary || item.source}</span>
                            <small>{formatMemoryTime(item.createdAt)}</small>
                          </div>
                        </div>
                      </article>
                    ))}
                    {memoryDraft.audit.length === 0 ? (
                      <div className="form-hint">暂无审计记录。</div>
                    ) : null}
                  </div>
                </section>
              </div>
            ) : null}

            {activeSection === "scheduled_tasks" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Scheduled</span>
                      <strong>定时任务列表</strong>
                    </div>
                    <button
                      className="ghost-button"
                      onClick={() => {
                        setEditingTask(null);
                        setTaskFormOpen(true);
                      }}
                      type="button"
                    >
                      新增任务
                    </button>
                  </div>

                  {taskFormOpen ? (
                    <ScheduledTaskForm
                      task={editingTask}
                      onSave={(data) => {
                        if (editingTask) {
                          onUpdateScheduledTask({ ...editingTask, ...data });
                        } else {
                          onCreateScheduledTask(data);
                        }
                        setTaskFormOpen(false);
                        setEditingTask(null);
                      }}
                      onCancel={() => {
                        setTaskFormOpen(false);
                        setEditingTask(null);
                      }}
                    />
                  ) : null}

                  <div className="config-list">
                    {scheduledTasks.length === 0 ? (
                      <div className="form-hint">暂无定时任务。</div>
                    ) : null}
                    {scheduledTasks.map((task) => (
                      <ConfigListItem
                        key={task.id}
                        enabled={task.enabled}
                        expanded={expandedTaskId === task.id}
                        onDelete={() => onDeleteScheduledTask(task.id)}
                        onToggleEnabled={(value) =>
                          onUpdateScheduledTask({ ...task, enabled: value })
                        }
                        onToggleExpanded={() => {
                          const next = expandedTaskId === task.id ? null : task.id;
                          setExpandedTaskId(next);
                          if (next) {
                            onRequestScheduledTaskHistory(task.id);
                            setHistoryTaskId(task.id);
                          }
                        }}
                        subtitle={`${task.scheduleExpr} · ${task.workerKind}`}
                        title={task.name || "未命名任务"}
                      >
                        <label className="form-field">
                          <span>名称</span>
                          <input
                            value={task.name}
                            onChange={(e) =>
                              onUpdateScheduledTask({ ...task, name: e.target.value })
                            }
                          />
                        </label>
                        <label className="form-field">
                          <span>执行指令</span>
                          <textarea
                            className="form-textarea"
                            value={task.prompt}
                            onChange={(e) =>
                              onUpdateScheduledTask({ ...task, prompt: e.target.value })
                            }
                          />
                        </label>
                        <label className="form-field">
                          <span>调度表达式 (Cron)</span>
                          <input
                            value={task.scheduleExpr}
                            onChange={(e) =>
                              onUpdateScheduledTask({ ...task, scheduleExpr: e.target.value })
                            }
                          />
                          <span className="form-hint">
                            如 0 20 * * * 表示每天20:00执行
                          </span>
                        </label>
                        <label className="form-field">
                          <span>Worker 类型</span>
                          <select
                            value={task.workerKind}
                            onChange={(e) =>
                              onUpdateScheduledTask({
                                ...task,
                                workerKind: e.target.value as ScheduledTask["workerKind"],
                              })
                            }
                          >
                            <option value="general">general</option>
                            <option value="coding">coding</option>
                            <option value="research">research</option>
                            <option value="solo">solo</option>
                          </select>
                        </label>
                        <div className="form-hint">
                          {task.nextRunAt ? (
                            <span>下次执行: {new Date(task.nextRunAt).toLocaleString()}</span>
                          ) : null}
                          {task.lastRunAt ? (
                            <span>上次执行: {new Date(task.lastRunAt).toLocaleString()}</span>
                          ) : null}
                        </div>
                        {historyTaskId === task.id ? (
                          <ScheduledTaskHistory
                            executions={scheduledTaskHistory[task.id] ?? []}
                          />
                        ) : null}
                      </ConfigListItem>
                    ))}
                  </div>
                </section>
              </div>
            ) : null}
          </div>
        </div>
      </aside>
    </>
  );
}

function ScheduledTaskForm(props: {
  task: ScheduledTask | null;
  onSave: (data: Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">
) => void;
  onCancel: () => void;
}) {
  const { task, onSave, onCancel } = props;
  const [name, setName] = useState(task?.name ?? "");
  const [prompt, setPrompt] = useState(task?.prompt ?? "");
  const [scheduleExpr, setScheduleExpr] = useState(task?.scheduleExpr ?? "0 20 * * *");
  const [workerKind, setWorkerKind] = useState<ScheduledTask["workerKind"]>(
    task?.workerKind ?? "general",
  );

  return (
    <div className="config-row-body" style={{ marginBottom: 12 }}>
      <label className="form-field">
        <span>任务名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="form-field">
        <span>执行指令</span>
        <textarea
          className="form-textarea"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="如：搜索今天的重要新闻并生成摘要"
        />
      </label>
      <label className="form-field">
        <span>调度表达式 (Cron)</span>
        <input
          value={scheduleExpr}
          onChange={(e) => setScheduleExpr(e.target.value)}
        />
        <span className="form-hint">如 0 20 * * * 表示每天20:00执行</span>
      </label>
      <label className="form-field">
        <span>Worker 类型</span>
        <select value={workerKind} onChange={(e) => setWorkerKind(e.target.value as ScheduledTask["workerKind"])}>
          <option value="general">general</option>
          <option value="coding">coding</option>
          <option value="research">research</option>
          <option value="solo">solo</option>
        </select>
      </label>
      <div className="inline-actions">
        <button
          className="ghost-button"
          onClick={() =>
            onSave({ name, prompt, scheduleExpr, workerKind, enabled: true, scheduleType: "cron" })
          }
          type="button"
          disabled={!name.trim() || !prompt.trim() || !scheduleExpr.trim()}
        >
          保存
        </button>
        <button className="ghost-button danger" onClick={onCancel} type="button">
          取消
        </button>
      </div>
    </div>
  );
}

function ScheduledTaskHistory(props: { executions: ScheduledTaskExecution[] }) {
  const { executions } = props;
  if (executions.length === 0) {
    return <div className="form-hint">暂无执行记录。</div>;
  }
  return (
    <div className="scheduled-task-history">
      {executions.map((exec) => (
        <div key={exec.id} className={`history-row ${exec.status}`}>
          <span className="history-time">{new Date(exec.startedAt).toLocaleString()}</span>
          <span className={`history-status ${exec.status}`}>
            {exec.status === "completed" ? "成功" : exec.status === "failed" ? "失败" : "执行中"}
          </span>
          {exec.result ? <pre className="history-result">{exec.result}</pre> : null}
          {exec.error ? <pre className="history-error">{exec.error}</pre> : null}
        </div>
      ))}
    </div>
  );
}
