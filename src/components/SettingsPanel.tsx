import { ThemeToggle } from "./ThemeToggle";
import type { AppSettings } from "../types/protocol";

interface SettingsPanelProps {
  settings: AppSettings;
  onChange: (settings: AppSettings) => void;
  onClose: () => void;
}

export function SettingsPanel(props: SettingsPanelProps) {
  const { settings, onChange, onClose } = props;

  return (
    <section className="settings-panel">
      <header className="panel-header">
        <div>
          <p className="eyebrow">集成配置</p>
          <h2>设置</h2>
        </div>
        <button className="ghost-action" onClick={onClose} type="button">
          返回对话
        </button>
      </header>

      <div className="settings-grid">
        <div className="settings-card">
          <h3>外观</h3>
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

        <div className="settings-card">
          <h3>模型配置</h3>
          <label className="field">
            <span>Provider</span>
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
              placeholder="例如 gpt-5-mini"
              value={settings.agent.modelId}
            />
          </label>
          <label className="field">
            <span>API Key</span>
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
              placeholder="OpenAI-like 时填写"
              value={settings.agent.baseUrl}
            />
          </label>
          <p className="field-hint">
            后端使用 Agno。`openai` 连接 OpenAI，`openai-like` 用于兼容 OpenAI API 的平台。
          </p>
        </div>

        <div className="settings-card">
          <h3>飞书机器人</h3>
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
              placeholder="预留字段"
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
              placeholder="预留字段"
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
              placeholder="预留字段"
              value={settings.feishu.verificationToken}
            />
          </label>
        </div>
      </div>
    </section>
  );
}
