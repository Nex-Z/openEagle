import { useEffect, useRef, useState } from "react";
import {
  CirclePlus,
  Feather,
  MoreHorizontal,
  Settings2,
  Trash2,
  Wrench,
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

function sidebarStatusCopy(backend: BackendState) {
  switch (backend.phase) {
    case "connected":
      return {
        primary: "本地服务在线",
        secondary: backend.port ? `端口 ${backend.port} · 刚刚同步` : "刚刚同步",
        tone: "success",
      };
    case "ready":
    case "connecting":
    case "starting":
      return {
        primary: "正在唤醒本地服务",
        secondary: "准备好后就能发送任务",
        tone: "warning",
      };
    case "error":
      return {
        primary: "本地服务需要检查",
        secondary: backend.message || "查看后端日志",
        tone: "danger",
      };
    default:
      return {
        primary: "本地服务同步中",
        secondary: backend.message || "稍后自动重连",
        tone: "neutral",
      };
  }
}

export function NavigationSidebar(props: NavigationSidebarProps) {
  const {
    conversations,
    activeConversationId,
    backend,
    mobileOpen,
    onSelectConversation,
    onDeleteConversation,
    onNewConversation,
    onOpenSettings,
    onCloseMobile,
  } = props;
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const statusCopy = sidebarStatusCopy(backend);

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
            <div className="brand-emblem" aria-hidden="true">
              <Feather size={19} />
            </div>
            <div className="brand-copy">
              <strong>openEagle</strong>
              <span className="brand-kicker">桌面 Agent 工作台</span>
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

        <footer className="nav-sidebar-footer">
          <div className="sidebar-quick-links" aria-label="工作台入口">
            <button className="secondary-button" onClick={() => onOpenSettings("tools")} type="button">
              <Wrench size={15} />
              <span>工具</span>
            </button>
            <button className="secondary-button" onClick={() => onOpenSettings("general")} type="button">
              <Settings2 size={15} />
              <span>设置</span>
            </button>
          </div>

          <div className={`status-card tone-${statusCopy.tone}`}>
            <div className="status-card-head">
              {backend.phase === "connected" ? <Wifi size={15} /> : <WifiOff size={15} />}
              <span>{statusCopy.primary}</span>
            </div>
            <small>{statusCopy.secondary}</small>
          </div>
        </footer>
      </aside>
    </>
  );
}
