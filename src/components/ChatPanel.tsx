import { useState } from "react";
import type { BackendState, ChatMessage } from "../types/protocol";

interface ChatPanelProps {
  backend: BackendState;
  messages: ChatMessage[];
  canSend: boolean;
  onSend: (content: string) => void;
}

export function ChatPanel(props: ChatPanelProps) {
  const { backend, messages, canSend, onSend } = props;
  const [draft, setDraft] = useState("");

  const submit = () => {
    const normalized = draft.trim();
    if (!normalized) {
      return;
    }
    onSend(normalized);
    setDraft("");
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.nativeEvent.isComposing) {
      return;
    }

    if (event.altKey) {
      return;
    }

    event.preventDefault();
    submit();
  };

  return (
    <section className="chat-panel">
      <header className="panel-header">
        <div>
          <p className="eyebrow">桌面智能体</p>
          <h2>主对话区</h2>
        </div>
      </header>

      <div className="message-stream">
        {messages.length === 0 ? (
          <div className="empty-state">
            <p>已连接后即可在这里发起任务、查看回复和追踪状态。</p>
            <small>左下角会持续显示后端服务状态，异常时可点击查看详情。</small>
          </div>
        ) : (
          messages.map((message) => (
            <article key={message.id} className={`message-card role-${message.role}`}>
              <div className="message-meta">
                <strong>
                  {message.role === "user"
                    ? "你"
                    : message.role === "assistant"
                      ? "Agent"
                      : "系统"}
                </strong>
                <span>{new Date(message.createdAt).toLocaleTimeString()}</span>
              </div>
              <p>{message.content}</p>
              {message.role === "assistant" && message.status === "pending" ? (
                <div className="message-thinking" aria-label="AI 正在思考">
                  <span />
                  <span />
                  <span />
                </div>
              ) : null}
            </article>
          ))
        )}
      </div>

      <div className="composer">
        <div className="composer-field">
          <textarea
            className="composer-input"
            disabled={!canSend}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              canSend
                ? "输入你要交给 openEagle 的任务..."
                : "等待后端启动完成..."
            }
            rows={3}
            value={draft}
          />
          <button
            aria-label="发送消息"
            className="send-action"
            disabled={!canSend || !draft.trim()}
            onClick={submit}
            type="button"
          >
            <svg aria-hidden="true" viewBox="0 0 24 24">
              <path d="M3.4 20.4 21 12 3.4 3.6l.1 6.1 10.6 2.3-10.6 2.3-.1 6.1Z" />
            </svg>
          </button>
        </div>
      </div>
    </section>
  );
}
