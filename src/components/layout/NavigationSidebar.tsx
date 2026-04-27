import { useEffect, useRef, useState } from "react";
import {
  CirclePlus,
  MoreHorizontal,
  Settings2,
  Sparkles,
  Trash2,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import type { BackendState, ConversationSummary } from "../../types/protocol";
import type { SettingsSection } from "../settings/SettingsDrawer";

interface NavigationSidebarProps {
  conversations: ConversationSummary[];
  activeConversationId: string;
  backend: BackendState;
  statusLine: string;
  statusDetail: string | null;
  mobileOpen: boolean;
  onSelectConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onNewConversation: () => void;
  onOpenSettings: (section: SettingsSection) => void;
  onCloseMobile: () => void;
}

export function NavigationSidebar(props: NavigationSidebarProps) {
  const {
    conversations,
    activeConversationId,
    backend,
    statusLine,
    statusDetail,
    mobileOpen,
    onSelectConversation,
    onDeleteConversation,
    onNewConversation,
    onOpenSettings,
    onCloseMobile,
  } = props;
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpenMenuId(null);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  return (
    <>
      <div
        className={mobileOpen ? "mobile-shell-backdrop is-visible" : "mobile-shell-backdrop"}
        onClick={onCloseMobile}
      />
      <aside
        ref={containerRef}
        className={mobileOpen ? "nav-sidebar mobile-open" : "nav-sidebar"}
      >
        <header className="nav-sidebar-header">
          <div className="brand-lockup">
            <div className="brand-emblem">OE</div>
            <div className="brand-copy">
              <span className="brand-kicker">桌面 Agent 工作台</span>
              <strong>openEagle</strong>
            </div>
          </div>
          {mobileOpen ? (
            <button
              aria-label="关闭侧边栏"
              className="icon-button mobile-only"
              onClick={onCloseMobile}
              type="button"
            >
              <X size={16} />
            </button>
          ) : null}
        </header>

        <button className="primary-button sidebar-create" onClick={onNewConversation} type="button">
          <CirclePlus size={16} />
          <span>新建会话</span>
        </button>

        <section className="nav-section nav-section-grow">
          <div className="section-heading">
            <span>会话</span>
            <small>{conversations.length}</small>
          </div>

          <div className="conversation-list">
            {conversations.map((conversation) => {
              const isActive = conversation.id === activeConversationId;
              return (
                <div
                  key={conversation.id}
                  className={isActive ? "conversation-row is-active" : "conversation-row"}
                >
                  <button
                    className="conversation-button"
                    onClick={() => {
                      onSelectConversation(conversation.id);
                      setOpenMenuId(null);
                    }}
                    type="button"
                  >
                    <span className="conversation-title">{conversation.title}</span>
                    <span className="conversation-time">
                      {new Date(conversation.updatedAt).toLocaleString()}
                    </span>
                  </button>

                  <div className="conversation-actions">
                    <button
                      aria-expanded={openMenuId === conversation.id}
                      aria-label={`打开 ${conversation.title} 的操作`}
                      className="conversation-menu-trigger"
                      onClick={(event) => {
                        event.stopPropagation();
                        setOpenMenuId((current) =>
                          current === conversation.id ? null : conversation.id,
                        );
                      }}
                      type="button"
                    >
                      <MoreHorizontal size={16} />
                    </button>

                    {openMenuId === conversation.id ? (
                      <div className="floating-menu" role="menu">
                        <button
                          className="floating-menu-item danger"
                          onClick={(event) => {
                            event.stopPropagation();
                            onDeleteConversation(conversation.id);
                            setOpenMenuId(null);
                          }}
                          role="menuitem"
                          type="button"
                        >
                          <Trash2 size={14} />
                          删除会话
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section className="nav-section">
          <div className="section-heading">
            <span>工作台</span>
          </div>

          <div className="sidebar-quick-links">
            <button className="secondary-button" onClick={() => onOpenSettings("general")} type="button">
              <Settings2 size={16} />
              <span>设置</span>
            </button>
            <button className="secondary-button" onClick={() => onOpenSettings("tools")} type="button">
              <Sparkles size={16} />
              <span>扩展</span>
            </button>
          </div>
        </section>

        <footer className="nav-sidebar-footer">
          <div className="status-card">
            <div className="status-card-head">
              {backend.phase === "connected" ? <Wifi size={15} /> : <WifiOff size={15} />}
              <span>{statusLine}</span>
            </div>
            <small>{statusDetail || backend.message}</small>
          </div>
        </footer>
      </aside>
    </>
  );
}
