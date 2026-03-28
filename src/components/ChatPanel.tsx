import { useState } from "react";
import type { BackendState, ChatMessage } from "../types/protocol";

interface ChatPanelProps {
  backend: BackendState;
  statusLine: string;
  messages: ChatMessage[];
  canSend: boolean;
  onSend: (content: string) => void;
}

export function ChatPanel(props: ChatPanelProps) {
  const { backend, statusLine, messages, canSend, onSend } = props;
  const [draft, setDraft] = useState("");

  const submit = () => {
    const normalized = draft.trim();
    if (!normalized) {
      return;
    }
    onSend(normalized);
    setDraft("");
  };

  return (
    <section className="chat-panel">
      <header className="panel-header">
        <div>
          <p className="eyebrow">桌面智能体</p>
          <h2>主对话区</h2>
        </div>
        <div className={`status-badge phase-${backend.phase}`}>
          <span className="status-dot" />
          {statusLine}
        </div>
      </header>

      <div className="message-stream">
        {messages.length === 0 ? (
          <div className="empty-state">
            <p>后端完成启动和握手后，就可以在这里开始对话。</p>
            <small>{backend.message}</small>
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
            </article>
          ))
        )}
      </div>

      <div className="composer">
        <textarea
          className="composer-input"
          disabled={!canSend}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={
            canSend
              ? "输入你要交给 openEagle 的任务..."
              : "等待后端启动完成..."
          }
          rows={4}
          value={draft}
        />
        <button
          className="send-action"
          disabled={!canSend || !draft.trim()}
          onClick={submit}
          type="button"
        >
          发送
        </button>
      </div>
    </section>
  );
}
