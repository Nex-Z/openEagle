import { useEffect, useRef, useState } from "react";
import type { BackendState, ConversationSummary } from "../types/protocol";

type SidebarView = "chat" | "general" | "tools" | "mcp" | "skills";

interface SidebarProps {
  conversations: ConversationSummary[];
  activeConversationId: string;
  activeView: SidebarView;
  backend: BackendState;
  statusLine: string;
  statusDetail: string | null;
  onSelectConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onNewConversation: () => void;
  onOpenSettings: (view: Exclude<SidebarView, "chat">) => void;
}

const configEntries: Array<{ id: Exclude<SidebarView, "chat">; label: string }> = [
  { id: "general", label: "基础设置" },
  { id: "tools", label: "工具" },
  { id: "mcp", label: "MCP" },
  { id: "skills", label: "Skill" },
];

export function Sidebar(props: SidebarProps) {
  const {
    conversations,
    activeConversationId,
    activeView,
    backend,
    statusLine,
    statusDetail,
    onSelectConversation,
    onDeleteConversation,
    onNewConversation,
    onOpenSettings,
  } = props;
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [showStatusDetail, setShowStatusDetail] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpenMenuId(null);
        setShowStatusDetail(false);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
    };
  }, []);

  useEffect(() => {
    if (!statusDetail) {
      setShowStatusDetail(false);
    }
  }, [statusDetail]);

  return (
    <aside ref={containerRef} className="sidebar-panel">
      <div className="brand-block">
        <div className="brand-mark">OE</div>
        <div>
          <p className="eyebrow">智能工作台</p>
          <h1>openEagle</h1>
        </div>
      </div>

      <button className="primary-action primary-action-compact" onClick={onNewConversation} type="button">
        新建会话
      </button>

      <div className="sidebar-section sidebar-section-compact">
        <p className="section-title">配置中心</p>
        <div className="workspace-nav-list">
          {configEntries.map((entry) => (
            <button
              key={entry.id}
              className={activeView === entry.id ? "workspace-nav-item active" : "workspace-nav-item"}
              onClick={() => {
                onOpenSettings(entry.id);
                setOpenMenuId(null);
              }}
              type="button"
            >
              <span>{entry.label}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="sidebar-section">
        <p className="section-title">历史会话</p>
        <div className="conversation-list">
          {conversations.map((conversation) => (
            <div
              key={conversation.id}
              className={
                conversation.id === activeConversationId
                  ? "conversation-row active"
                  : "conversation-row"
              }
            >
              <button
                className="conversation-card"
                onClick={() => {
                  onSelectConversation(conversation.id);
                  setOpenMenuId(null);
                }}
                type="button"
              >
                <span>{conversation.title}</span>
                <small>{new Date(conversation.updatedAt).toLocaleString()}</small>
              </button>

              <div className="conversation-actions">
                <button
                  aria-expanded={openMenuId === conversation.id}
                  aria-haspopup="menu"
                  aria-label={`打开 ${conversation.title} 的菜单`}
                  className="conversation-menu-trigger"
                  onClick={(event) => {
                    event.stopPropagation();
                    setOpenMenuId((current) =>
                      current === conversation.id ? null : conversation.id,
                    );
                  }}
                  type="button"
                >
                  <svg aria-hidden="true" viewBox="0 0 24 24">
                    <path d="M12 7.25a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5Zm0 6.5a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5Zm0 6.5a1.75 1.75 0 1 1 0-3.5 1.75 1.75 0 0 1 0 3.5Z" />
                  </svg>
                </button>

                {openMenuId === conversation.id ? (
                  <div className="conversation-menu" role="menu">
                    <button
                      className="conversation-menu-item danger"
                      onClick={(event) => {
                        event.stopPropagation();
                        onDeleteConversation(conversation.id);
                        setOpenMenuId(null);
                      }}
                      role="menuitem"
                      type="button"
                    >
                      删除会话
                    </button>
                  </div>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="sidebar-footer">
        <div className="sidebar-status-group">
          <button
            className={`status-badge phase-${backend.phase} sidebar-status ${statusDetail ? "is-clickable" : ""}`}
            disabled={!statusDetail}
            onClick={() => {
              if (statusDetail) {
                setShowStatusDetail((current) => !current);
              }
            }}
            type="button"
          >
            <span className="status-dot" />
            <span>{statusLine}</span>
          </button>

          {showStatusDetail && statusDetail ? (
            <div className="sidebar-status-detail status-detail-card" role="status">
              <div className="status-detail-header">
                <strong>异常详情</strong>
                <button
                  className="detail-close"
                  onClick={() => setShowStatusDetail(false)}
                  type="button"
                >
                  关闭
                </button>
              </div>
              <p>{statusDetail}</p>
            </div>
          ) : null}
        </div>

        <button
          aria-label="打开设置"
          className="icon-action"
          onClick={() => onOpenSettings("general")}
          title="设置"
          type="button"
        >
          <svg aria-hidden="true" viewBox="0 0 24 24">
            <path d="M19.14 12.94a7.43 7.43 0 0 0 .05-.94 7.43 7.43 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.63l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.28 7.28 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.49-.42h-3.84a.5.5 0 0 0-.49.42l-.36 2.54c-.58.22-1.13.54-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.71 8.85a.5.5 0 0 0 .12.63l2.03 1.58a7.43 7.43 0 0 0-.05.94 7.43 7.43 0 0 0 .05.94L2.83 14.52a.5.5 0 0 0-.12.63l1.92 3.32a.5.5 0 0 0 .6.22l2.39-.96c.5.4 1.05.72 1.63.94l.36 2.54a.5.5 0 0 0 .49.42h3.84a.5.5 0 0 0 .49-.42l.36-2.54c.58-.22 1.13-.54 1.63-.94l2.39.96a.5.5 0 0 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.63l-2.03-1.58ZM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7Z" />
          </svg>
        </button>
      </div>
    </aside>
  );
}
