import { RefreshCw } from "lucide-react";
import type { TokenUsageDashboard, TokenUsageSummary } from "../../types/protocol";

interface TokenUsagePanelProps {
  usage: TokenUsageDashboard;
  onRefresh: () => boolean;
}

const sourceLabels: Record<string, string> = {
  chat: "客户端",
  remote: "远程 IM",
  solo: "桌面执行",
  scheduled: "定时任务",
};

function formatTokens(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatCompactTokens(value: number) {
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatDate(value: string, options?: Intl.DateTimeFormatOptions) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function UsageBreakdown({ usage }: { usage: TokenUsageSummary }) {
  return (
    <div className="token-usage-breakdown">
      <span>输入 {formatTokens(usage.inputTokens)}</span>
      <span>输出 {formatTokens(usage.outputTokens)}</span>
      <span>{formatTokens(usage.calls)} 次调用</span>
    </div>
  );
}

export function TokenUsagePanel({ usage, onRefresh }: TokenUsagePanelProps) {
  const maxDayTokens = Math.max(1, ...usage.days.map((day) => day.totalTokens));
  const maxModelTokens = Math.max(1, ...usage.models.map((model) => model.totalTokens));

  return (
    <div className="settings-stack token-usage-dashboard">
      <section className="settings-panel token-usage-hero">
        <div className="settings-panel-head">
          <div>
            <span className="card-kicker">Token usage</span>
            <strong>累计消耗</strong>
          </div>
          <button className="ghost-button" onClick={onRefresh} type="button">
            <RefreshCw size={14} />
            刷新
          </button>
        </div>
        <div className="token-usage-total">
          <strong>{formatTokens(usage.total.totalTokens)}</strong>
          <span>tokens</span>
        </div>
        <UsageBreakdown usage={usage.total} />
        <div className="token-usage-today">
          <span>今日</span>
          <strong>{formatTokens(usage.today.totalTokens)}</strong>
          <small>tokens · {usage.today.calls} 次调用</small>
        </div>
      </section>

      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <span className="card-kicker">Last 7 days</span>
            <strong>近七日趋势</strong>
          </div>
        </div>
        <div className="token-usage-days">
          {usage.days.map((day) => (
            <div className="token-usage-day" key={day.date}>
              <span>{formatDate(day.date, { weekday: "short" })}</span>
              <div className="token-usage-bar-track">
                <i style={{ width: `${(day.totalTokens / maxDayTokens) * 100}%` }} />
              </div>
              <strong>{formatCompactTokens(day.totalTokens)}</strong>
            </div>
          ))}
          {usage.days.length === 0 ? (
            <p className="token-usage-empty">还没有可展示的调用记录。</p>
          ) : null}
        </div>
      </section>

      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <span className="card-kicker">Models</span>
            <strong>模型分布</strong>
          </div>
        </div>
        <div className="token-usage-models">
          {usage.models.map((model) => (
            <div className="token-usage-model" key={`${model.provider}:${model.model}`}>
              <div className="token-usage-model-copy">
                <strong>{model.model}</strong>
                <span>{model.provider} · {model.calls} 次调用</span>
              </div>
              <div className="token-usage-model-meter">
                <i style={{ width: `${(model.totalTokens / maxModelTokens) * 100}%` }} />
              </div>
              <span>{formatTokens(model.totalTokens)}</span>
            </div>
          ))}
          {usage.models.length === 0 ? (
            <p className="token-usage-empty">模型返回 usage 后会在这里出现。</p>
          ) : null}
        </div>
      </section>

      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <span className="card-kicker">Recent tasks</span>
            <strong>最近任务</strong>
          </div>
        </div>
        <div className="token-usage-recent">
          {usage.recentRequests.map((request) => (
            <div className="token-usage-request" key={request.requestId}>
              <div>
                <strong>{sourceLabels[request.source] ?? request.source}</strong>
                <span>
                  {formatDate(request.updatedAt, {
                    month: "numeric",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                  {request.models.length > 0 ? ` · ${request.models.join(", ")}` : ""}
                </span>
              </div>
              <div className="token-usage-request-total">
                <strong>{formatTokens(request.totalTokens)}</strong>
                <span>
                  输入 {formatCompactTokens(request.inputTokens)} · 输出{" "}
                  {formatCompactTokens(request.outputTokens)}
                </span>
              </div>
            </div>
          ))}
          {usage.recentRequests.length === 0 ? (
            <p className="token-usage-empty">完成一次 AI 任务后，这里会显示实际消耗。</p>
          ) : null}
        </div>
        <p className="token-usage-note">仅统计模型供应商实际返回的 usage 数据，不进行估算。</p>
      </section>
    </div>
  );
}
