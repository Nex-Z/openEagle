import { useEffect, useState, type CSSProperties } from "react";
import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { Monitor, SlidersHorizontal, Sparkles, Wrench, X } from "lucide-react";
import { ThemeToggle } from "../ThemeToggle";
import type {
  AppSettings,
  McpServerConfig,
  SkillConfig,
  SoloDisplayOption,
  ToolConfig,
} from "../../types/protocol";
import { SecretInput } from "./SecretInput";

export type SettingsSection =
  | "general"
  | "models"
  | "solo"
  | "tools"
  | "mcp"
  | "skills";

interface SettingsDrawerProps {
  open: boolean;
  settings: AppSettings;
  activeSection: SettingsSection;
  soloDisplays: SoloDisplayOption[];
  onRefreshSoloDisplays: () => boolean;
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
  { id: "solo", title: "SOLO", summary: "显示器预览与截图目标。" },
  { id: "tools", title: "Tools", summary: "本地工具入口。" },
  { id: "mcp", title: "MCP", summary: "MCP Server 配置。" },
  { id: "skills", title: "Skills", summary: "提示技能配置。" },
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
            <span>{enabled ? "开" : "关"}</span>
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
  const {
    open,
    settings,
    activeSection,
    soloDisplays,
    onRefreshSoloDisplays,
    onChange,
    onClose,
    onSectionChange,
  } = props;
  const [expandedToolId, setExpandedToolId] = useState<string | null>(null);
  const [expandedMcpId, setExpandedMcpId] = useState<string | null>(null);
  const [expandedSkillId, setExpandedSkillId] = useState<string | null>(null);
  const [previewDataUrls, setPreviewDataUrls] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open && activeSection === "solo") {
      onRefreshSoloDisplays();
    }
  }, [activeSection, onRefreshSoloDisplays, open]);

  useEffect(() => {
    const previewPaths = soloDisplays
      .map((display) => display.previewPath)
      .filter(Boolean) as string[];
    const missing = previewPaths.filter((path) => !previewDataUrls[path]);
    if (missing.length === 0) {
      return;
    }

    let cancelled = false;
    void Promise.all(
      missing.map(async (path) => {
        try {
          const dataUrl = await invoke<string>("read_image_data_url", { path });
          return { path, dataUrl };
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) {
        return;
      }
      setPreviewDataUrls((current) => {
        const next = { ...current };
        for (const entry of entries) {
          if (entry) {
            next[entry.path] = entry.dataUrl;
          }
        }
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [previewDataUrls, soloDisplays]);

  const activeMeta = sectionMeta.find((section) => section.id === activeSection) ?? sectionMeta[0];

  return (
    <>
      <div className={open ? "settings-backdrop is-visible" : "settings-backdrop"} onClick={onClose} />
      <aside className={open ? "settings-drawer is-open" : "settings-drawer"}>
        <div className="settings-drawer-nav">
          <div className="settings-drawer-brand">
            <div className="brand-emblem small">OE</div>
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
              <div className="settings-stack">
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
                      <span className="card-kicker">接入</span>
                      <strong>飞书机器人</strong>
                    </div>
                    <Sparkles size={16} />
                  </div>
                  <label className="form-switch">
                    <span>启用飞书入口</span>
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
                  </label>
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
                  <label className="form-field">
                    <span>Verification Token</span>
                    <input
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          feishu: {
                            ...settings.feishu,
                            verificationToken: event.target.value,
                          },
                        })
                      }
                      value={settings.feishu.verificationToken}
                    />
                  </label>
                </section>
              </div>
            ) : null}

            {activeSection === "models" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">文本模型</span>
                      <strong>Chat / Tool 推理</strong>
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
                      <strong>SOLO 执行</strong>
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
              <div className="settings-stack">
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
                            {display.previewPath ? (
                              <img
                                alt={display.label}
                                src={
                                  previewDataUrls[display.previewPath] ||
                                  (display.previewPath.startsWith("data:")
                                    ? display.previewPath
                                    : convertFileSrc(display.previewPath))
                                }
                                style={previewStyle}
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
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">执行入口</span>
                      <strong>工具列表</strong>
                    </div>
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
                </section>
              </div>
            ) : null}

            {activeSection === "mcp" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Model Context Protocol</span>
                      <strong>MCP Server</strong>
                    </div>
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
                          </select>
                        </label>
                        <label className="form-field">
                          <span>端点 / 启动命令</span>
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
                </section>
              </div>
            ) : null}

            {activeSection === "skills" ? (
              <div className="settings-stack">
                <section className="settings-panel">
                  <div className="settings-panel-head">
                    <div>
                      <span className="card-kicker">Prompt Skills</span>
                      <strong>Skill 列表</strong>
                    </div>
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
                </section>
              </div>
            ) : null}
          </div>
        </div>
      </aside>
    </>
  );
}
