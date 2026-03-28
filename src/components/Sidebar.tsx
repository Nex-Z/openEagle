import type { ReactNode } from "react";
import type { ConversationSummary } from "../types/protocol";

interface SidebarProps {
  conversations: ConversationSummary[];
  activeConversationId: string;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => void;
  onOpenSettings: () => void;
  quickTheme: ReactNode;
}

export function Sidebar(props: SidebarProps) {
  const {
    conversations,
    activeConversationId,
    onSelectConversation,
    onNewConversation,
    onOpenSettings,
    quickTheme,
  } = props;

  return (
    <aside className="sidebar-panel">
      <div className="brand-block">
        <div className="brand-mark">OE</div>
        <div>
          <p className="eyebrow">智能工作台</p>
          <h1>openEagle</h1>
        </div>
      </div>

      <button className="primary-action" onClick={onNewConversation} type="button">
        新建对话
      </button>

      <div className="sidebar-section">
        <p className="section-title">历史会话</p>
        <div className="conversation-list">
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={
                conversation.id === activeConversationId
                  ? "conversation-card active"
                  : "conversation-card"
              }
              onClick={() => onSelectConversation(conversation.id)}
              type="button"
            >
              <span>{conversation.title}</span>
              <small>{new Date(conversation.updatedAt).toLocaleString()}</small>
            </button>
          ))}
        </div>
      </div>

      <div className="sidebar-footer">
        {quickTheme}
        <button className="ghost-action" onClick={onOpenSettings} type="button">
          设置
        </button>
      </div>
    </aside>
  );
}
