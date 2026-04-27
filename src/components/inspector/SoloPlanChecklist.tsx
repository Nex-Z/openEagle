import { CheckCircle2, Circle, Loader2, MinusCircle, XCircle } from "lucide-react";
import type { SoloPlanStatus } from "../../types/protocol";

interface SoloPlanChecklistProps {
  plan: SoloPlanStatus;
}

const STATUS_CONFIG = {
  completed: { icon: CheckCircle2, color: "var(--color-success, #22c55e)", label: "完成" },
  in_progress: { icon: Loader2, color: "var(--color-info, #3b82f6)", label: "进行中" },
  pending: { icon: Circle, color: "var(--color-muted, #6b7280)", label: "等待" },
  failed: { icon: XCircle, color: "var(--color-danger, #ef4444)", label: "失败" },
  skipped: { icon: MinusCircle, color: "var(--color-muted, #6b7280)", label: "跳过" },
} as const;

export function SoloPlanChecklist({ plan }: SoloPlanChecklistProps) {
  const completedCount = plan.items.filter((i) => i.status === "completed").length;
  const totalCount = plan.items.length;

  return (
    <section className="inspector-card solo-plan-card">
      <div className="inspector-card-head">
        <div>
          <span className="card-kicker">执行计划</span>
          <strong>
            {completedCount}/{totalCount} 步
          </strong>
        </div>
        {plan.replanCount > 0 && (
          <span className="status-badge tone-warning">重新规划 x{plan.replanCount}</span>
        )}
      </div>

      {plan.taskAnalysis && (
        <div className="plan-analysis">{plan.taskAnalysis}</div>
      )}

      {plan.agentMessage && (
        <div className="plan-agent-message">{plan.agentMessage}</div>
      )}

      <div className="plan-checklist">
        {plan.items.map((item) => {
          const config = STATUS_CONFIG[item.status] ?? STATUS_CONFIG.pending;
          const Icon = config.icon;
          return (
            <div
              key={item.index}
              className={`plan-item plan-item-${item.status}`}
            >
              <Icon
                size={16}
                className={`plan-item-icon ${item.status === "in_progress" ? "spin" : ""}`}
                style={{ color: config.color, flexShrink: 0 }}
              />
              <span className="plan-item-desc">{item.description || item.action}</span>
              <span className="plan-item-action">{item.action}</span>
            </div>
          );
        })}
      </div>

      {plan.alternative && plan.alternative !== "无" && (
        <div className="plan-alternative">
          替代方案: {plan.alternative}
        </div>
      )}
    </section>
  );
}
