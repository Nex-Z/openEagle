import { useEffect, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { invoke } from "@tauri-apps/api/core";
import type {
  AppSettings,
  McpServerConfig,
  SoloDisplayOption,
  SkillConfig,
  ToolConfig,
} from "../types/protocol";
import { ThemeToggle } from "./ThemeToggle";

type SettingsSection = "general" | "tools" | "mcp" | "skills";

interface SettingsPanelProps {
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
  eyebrow: string;
  title: string;
  summary: string;
}> = [
  {
    id: "general",
    eyebrow: "集成配置",
    title: "基础设置",
    summary: "统一管理外观、模型接入和飞书入口。",
  },
  {
    id: "tools",
    eyebrow: "扩展能力",
    title: "工具配置",
    summary: "管理本地工具、命令入口和执行说明。",
  },
  {
    id: "mcp",
    eyebrow: "扩展能力",
    title: "MCP 配置",
    summary: "配置 MCP Server 的传输方式、端点和用途说明。",
  },
  {
    id: "skills",
    eyebrow: "扩展能力",
    title: "Skill 配置",
    summary: "维护技能定义、说明和提示词。",
  },
];

function createToolConfig(): ToolConfig {
  return {
    id: crypto.randomUUID(),
    name: "新工具",
    description: "",
    command: "",
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

function renderGeneralSection(
  settings: AppSettings,
  onChange: (settings: AppSettings) => void,
  soloDisplays: SoloDisplayOption[],
  onRefreshSoloDisplays: () => boolean,
) {
  return (
    <div className="settings-grid">
      <div className="settings-column">
        <div className="settings-card settings-card-compact">
          <div className="settings-card-heading">
            <div>
              <p className="eyebrow">界面</p>
              <h3>外观</h3>
            </div>
          </div>
          <label className="field">
            <span>主题模式</span>
            <ThemeToggle
              onChange={(themeMode) =>
                onChange({
                  ...settings,
                  appearance: { themeMode },
                })
              }
              value={settings.appearance.themeMode}
            />
          </label>
          <p className="field-hint">支持固定日间、固定夜间，或自动跟随系统主题。</p>
        </div>

        <div className="settings-card settings-card-compact">
          <div className="settings-card-heading">
            <div>
              <p className="eyebrow">接入</p>
              <h3>飞书机器人</h3>
            </div>
          </div>
          <label className="switch-row">
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

          <label className="field">
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
              placeholder="飞书应用的 App ID"
              value={settings.feishu.appId}
            />
          </label>

          <label className="field">
            <span>App Secret</span>
            <input
              onChange={(event) =>
                onChange({
                  ...settings,
                  feishu: {
                    ...settings.feishu,
                    appSecret: event.target.value,
                  },
                })
              }
              placeholder="飞书应用的 App Secret"
              value={settings.feishu.appSecret}
            />
          </label>

          <label className="field">
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
              placeholder="事件订阅校验 Token"
              value={settings.feishu.verificationToken}
            />
          </label>
        </div>
      </div>

      <div className="settings-card settings-card-feature">
        <div className="settings-card-heading">
          <div>
            <p className="eyebrow">模型</p>
            <h3>模型配置（文本 + VL）</h3>
          </div>
          <span className="settings-pill">{settings.agent.provider}</span>
        </div>
        <p className="field-hint">文本模型用于普通对话与工具推理。</p>
        <label className="field">
          <span>文本 Provider</span>
          <select
            value={settings.agent.provider}
            onChange={(event) =>
              onChange({
                ...settings,
                agent: {
                  ...settings.agent,
                  provider: event.target.value as AppSettings["agent"]["provider"],
                },
              })
            }
          >
            <option value="mock">mock</option>
            <option value="openai">openai</option>
            <option value="openai-like">openai-like</option>
          </select>
        </label>
        <label className="field">
          <span>文本模型 ID</span>
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
            placeholder="例如 gpt-5-mini"
            value={settings.agent.modelId}
          />
        </label>
        <label className="field">
          <span>文本 API Key</span>
          <input
            onChange={(event) =>
              onChange({
                ...settings,
                agent: {
                  ...settings.agent,
                  apiKey: event.target.value,
                },
              })
            }
            placeholder="OpenAI 或兼容平台的 API Key"
            type="password"
            value={settings.agent.apiKey}
          />
        </label>
        <label className="field">
          <span>文本 Base URL</span>
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
            placeholder="OpenAI-like 时填写"
            value={settings.agent.baseUrl}
          />
        </label>

        <hr className="settings-divider" />
        <p className="field-hint">VL 模型用于 SOLO 视觉操作，未配置时无法启动 SOLO。</p>
        <label className="field">
          <span>VL Provider</span>
          <select
            value={settings.agent.vlProvider}
            onChange={(event) =>
              onChange({
                ...settings,
                agent: {
                  ...settings.agent,
                  vlProvider: event.target.value as AppSettings["agent"]["vlProvider"],
                },
              })
            }
          >
            <option value="openai">openai</option>
            <option value="openai-like">openai-like</option>
          </select>
        </label>
        <label className="field">
          <span>VL 模型 ID</span>
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
            placeholder="例如 gpt-4.1-mini"
            value={settings.agent.vlModelId}
          />
        </label>
        <label className="field">
          <span>VL API Key</span>
          <input
            onChange={(event) =>
              onChange({
                ...settings,
                agent: {
                  ...settings.agent,
                  vlApiKey: event.target.value,
                },
              })
            }
            placeholder="用于视觉理解模型调用"
            type="password"
            value={settings.agent.vlApiKey}
          />
        </label>
        <label className="field">
          <span>VL Base URL</span>
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
            placeholder="VL 为 openai-like 时填写"
            value={settings.agent.vlBaseUrl}
          />
        </label>
        <p className="field-hint">
          后端使用 Agno。`openai` 连接 OpenAI，`openai-like` 用于兼容 OpenAI API 的平台。
        </p>

        <hr className="settings-divider" />
        <div className="settings-card-heading">
          <div>
            <p className="eyebrow">SOLO</p>
            <h3>截图显示器</h3>
          </div>
          <button className="secondary-action" onClick={onRefreshSoloDisplays} type="button">
            刷新预览
          </button>
        </div>
        <p className="field-hint">
          选择 SOLO 截图和坐标执行使用的显示器。截图预览来自实时采样。
        </p>
        {soloDisplays.length === 0 ? (
          <div className="solo-display-empty">暂无显示器预览，点击“刷新预览”获取。</div>
        ) : (
          <div className="solo-display-grid">
            {soloDisplays.map((display) => {
              const isActive = settings.solo.preferredDisplayIndex === display.index;
              return (
                <label
                  key={display.index}
                  className={isActive ? "solo-display-card active" : "solo-display-card"}
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
                  <div className="solo-display-head">
                    <strong>{display.label}</strong>
                    {display.isPrimary ? <span className="settings-pill">主屏</span> : null}
                  </div>
                  <small>
                    {display.width}×{display.height} · ({display.left}, {display.top})
                  </small>
                  {display.previewPath ? (
                    <img
                      alt={`${display.label} 预览`}
                      className="solo-display-preview"
                      src={
                        display.previewPath.startsWith("data:")
                          ? display.previewPath
                          : convertFileSrc(display.previewPath)
                      }
                    />
                  ) : (
                    <div className="solo-display-preview solo-display-preview-empty">
                      无预览图
                    </div>
                  )}
                </label>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

interface ToolListProps {
  settings: AppSettings;
  expandedId: string | null;
  onChange: (settings: AppSettings) => void;
  onToggleExpand: (id: string) => void;
}

function renderToolsSection(props: ToolListProps) {
  const { settings, expandedId, onChange, onToggleExpand } = props;

  return (
    <div className="settings-stack">
      <div className="settings-card settings-card-tool-builtin">
        <div className="settings-card-heading">
          <div>
            <p className="eyebrow">系统内置</p>
            <h3>截图工具</h3>
          </div>
          <span className="toggle-chip">已启用</span>
        </div>
        <p className="field-hint">
          SOLO 模式会调用系统级截图能力（不经过用户自定义命令）。
        </p>
      </div>

      <div className="settings-card settings-card-toolbar">
        <div>
          <p className="eyebrow">执行入口</p>
          <h3>工具列表</h3>
        </div>
        <button
          className="secondary-action"
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

      <div className="settings-list">
        {settings.tools.map((tool) => {
          const isExpanded = expandedId === tool.id;
          return (
            <article key={tool.id} className="settings-list-item">
              <div className="settings-list-row">
                <div className="settings-list-main">
                  <strong>{tool.name || "未命名工具"}</strong>
                  <span className="settings-list-meta">
                    {tool.command || "未配置命令"}
                  </span>
                </div>

                <div className="settings-list-actions">
                  <label className="switch-inline">
                    <input
                      checked={tool.enabled}
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          tools: updateListItem(settings.tools, tool.id, (item) => ({
                            ...item,
                            enabled: event.target.checked,
                          })),
                        })
                      }
                      type="checkbox"
                    />
                    <span>{tool.enabled ? "开" : "关"}</span>
                  </label>
                  <button
                    className="text-action"
                    onClick={() => onToggleExpand(tool.id)}
                    type="button"
                  >
                    {isExpanded ? "收起" : "编辑"}
                  </button>
                  <button
                    className="text-action danger"
                    onClick={() =>
                      onChange({
                        ...settings,
                        tools: removeListItem(settings.tools, tool.id),
                      })
                    }
                    type="button"
                  >
                    删除
                  </button>
                </div>
              </div>

              {isExpanded ? (
                <div className="settings-list-editor">
                  <div className="settings-item-grid">
                    <label className="field">
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
                        placeholder="例如 Git Tool"
                        value={tool.name}
                      />
                    </label>
                    <label className="field">
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
                        placeholder="例如 npx my-tool"
                        value={tool.command}
                      />
                    </label>
                  </div>

                  <label className="field">
                    <span>说明</span>
                    <textarea
                      className="field-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          tools: updateListItem(settings.tools, tool.id, (item) => ({
                            ...item,
                            description: event.target.value,
                          })),
                        })
                      }
                      placeholder="说明这个工具解决什么问题、适合什么时候调用。"
                      value={tool.description}
                    />
                  </label>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </div>
  );
}

interface McpListProps {
  settings: AppSettings;
  expandedId: string | null;
  onChange: (settings: AppSettings) => void;
  onToggleExpand: (id: string) => void;
}

function renderMcpSection(props: McpListProps) {
  const { settings, expandedId, onChange, onToggleExpand } = props;

  return (
    <div className="settings-stack">
      <div className="settings-card settings-card-toolbar">
        <div>
          <p className="eyebrow">Model Context Protocol</p>
          <h3>MCP Server 列表</h3>
        </div>
        <button
          className="secondary-action"
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

      <div className="settings-list">
        {settings.mcp.map((server) => {
          const isExpanded = expandedId === server.id;
          return (
            <article key={server.id} className="settings-list-item">
              <div className="settings-list-row">
                <div className="settings-list-main">
                  <strong>{server.name || "未命名 MCP"}</strong>
                  <span className="settings-list-meta">
                    {server.transport} · {server.endpoint || "未配置端点"}
                  </span>
                </div>

                <div className="settings-list-actions">
                  <label className="switch-inline">
                    <input
                      checked={server.enabled}
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          mcp: updateListItem(settings.mcp, server.id, (item) => ({
                            ...item,
                            enabled: event.target.checked,
                          })),
                        })
                      }
                      type="checkbox"
                    />
                    <span>{server.enabled ? "开" : "关"}</span>
                  </label>
                  <button
                    className="text-action"
                    onClick={() => onToggleExpand(server.id)}
                    type="button"
                  >
                    {isExpanded ? "收起" : "编辑"}
                  </button>
                  <button
                    className="text-action danger"
                    onClick={() =>
                      onChange({
                        ...settings,
                        mcp: removeListItem(settings.mcp, server.id),
                      })
                    }
                    type="button"
                  >
                    删除
                  </button>
                </div>
              </div>

              {isExpanded ? (
                <div className="settings-list-editor">
                  <div className="settings-item-grid">
                    <label className="field">
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
                        placeholder="例如 Browser MCP"
                        value={server.name}
                      />
                    </label>
                    <label className="field">
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
                  </div>

                  <label className="field">
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
                      placeholder="例如 http://localhost:3001 或 npx ..."
                      value={server.endpoint}
                    />
                  </label>

                  <label className="field">
                    <span>说明</span>
                    <textarea
                      className="field-textarea"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          mcp: updateListItem(settings.mcp, server.id, (item) => ({
                            ...item,
                            description: event.target.value,
                          })),
                        })
                      }
                      placeholder="说明这个 MCP Server 暴露哪些能力。"
                      value={server.description}
                    />
                  </label>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </div>
  );
}

interface SkillListProps {
  settings: AppSettings;
  expandedId: string | null;
  onChange: (settings: AppSettings) => void;
  onToggleExpand: (id: string) => void;
}

function renderSkillsSection(props: SkillListProps) {
  const { settings, expandedId, onChange, onToggleExpand } = props;

  return (
    <div className="settings-stack">
      <div className="settings-card settings-card-toolbar">
        <div>
          <p className="eyebrow">Prompt Skills</p>
          <h3>Skill 列表</h3>
        </div>
        <button
          className="secondary-action"
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

      <div className="settings-list">
        {settings.skills.map((skill) => {
          const isExpanded = expandedId === skill.id;
          return (
            <article key={skill.id} className="settings-list-item">
              <div className="settings-list-row">
                <div className="settings-list-main">
                  <strong>{skill.name || "未命名 Skill"}</strong>
                  <span className="settings-list-meta">
                    {skill.description || "未填写说明"}
                  </span>
                </div>

                <div className="settings-list-actions">
                  <label className="switch-inline">
                    <input
                      checked={skill.enabled}
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          skills: updateListItem(settings.skills, skill.id, (item) => ({
                            ...item,
                            enabled: event.target.checked,
                          })),
                        })
                      }
                      type="checkbox"
                    />
                    <span>{skill.enabled ? "开" : "关"}</span>
                  </label>
                  <button
                    className="text-action"
                    onClick={() => onToggleExpand(skill.id)}
                    type="button"
                  >
                    {isExpanded ? "收起" : "编辑"}
                  </button>
                  <button
                    className="text-action danger"
                    onClick={() =>
                      onChange({
                        ...settings,
                        skills: removeListItem(settings.skills, skill.id),
                      })
                    }
                    type="button"
                  >
                    删除
                  </button>
                </div>
              </div>

              {isExpanded ? (
                <div className="settings-list-editor">
                  <div className="settings-item-grid">
                    <label className="field">
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
                        placeholder="例如 Code Review Skill"
                        value={skill.name}
                      />
                    </label>
                    <label className="field">
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
                        placeholder="简要说明技能职责"
                        value={skill.description}
                      />
                    </label>
                  </div>

                  <label className="field">
                    <span>提示词</span>
                    <textarea
                      className="field-textarea field-textarea-lg"
                      onChange={(event) =>
                        onChange({
                          ...settings,
                          skills: updateListItem(settings.skills, skill.id, (item) => ({
                            ...item,
                            prompt: event.target.value,
                          })),
                        })
                      }
                      placeholder="填写触发该 Skill 时希望注入给 Agent 的提示。"
                      value={skill.prompt}
                    />
                  </label>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </div>
  );
}

export function SettingsPanel(props: SettingsPanelProps) {
  const {
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
    if (activeSection === "general") {
      onRefreshSoloDisplays();
    }
  }, [activeSection, onRefreshSoloDisplays]);

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
          if (!entry) {
            continue;
          }
          next[entry.path] = entry.dataUrl;
        }
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [previewDataUrls, soloDisplays]);

  const activeMeta =
    sectionMeta.find((section) => section.id === activeSection) ?? sectionMeta[0];

  return (
    <section className="settings-panel">
      <header className="panel-header">
        <div className="settings-header-copy">
          <p className="eyebrow">{activeMeta.eyebrow}</p>
          <h2>{activeMeta.title}</h2>
          <p className="settings-summary">{activeMeta.summary}</p>
        </div>
        <button className="secondary-action" onClick={onClose} type="button">
          返回对话
        </button>
      </header>

      <div className="settings-section-tabs">
        {sectionMeta.map((section) => (
          <button
            key={section.id}
            className={
              section.id === activeSection
                ? "settings-section-tab active"
                : "settings-section-tab"
            }
            onClick={() => onSectionChange(section.id)}
            type="button"
          >
            {section.title}
          </button>
        ))}
      </div>

      {activeSection === "general"
        ? renderGeneralSection(
            settings,
            onChange,
            soloDisplays.map((display) => ({
              ...display,
              previewPath:
                (display.previewPath && previewDataUrls[display.previewPath]) ||
                display.previewPath,
            })),
            onRefreshSoloDisplays,
          )
        : null}
      {activeSection === "tools"
        ? renderToolsSection({
            settings,
            expandedId: expandedToolId,
            onChange,
            onToggleExpand: (id) =>
              setExpandedToolId((current) => (current === id ? null : id)),
          })
        : null}
      {activeSection === "mcp"
        ? renderMcpSection({
            settings,
            expandedId: expandedMcpId,
            onChange,
            onToggleExpand: (id) =>
              setExpandedMcpId((current) => (current === id ? null : id)),
          })
        : null}
      {activeSection === "skills"
        ? renderSkillsSection({
            settings,
            expandedId: expandedSkillId,
            onChange,
            onToggleExpand: (id) =>
              setExpandedSkillId((current) => (current === id ? null : id)),
          })
        : null}
    </section>
  );
}
