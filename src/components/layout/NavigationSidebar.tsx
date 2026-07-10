import { memo, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CirclePlus,
  Feather,
  MoreHorizontal,
  Settings2,
  Trash2,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import type { BackendState, ConversationSummary } from "../../types/protocol";
import { ConfirmationDialog } from "../ConfirmationDialog";
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

function NavigationSidebarComponent(props: NavigationSidebarProps) {
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
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const [pendingConversationDelete, setPendingConversationDelete] = useState<ConversationSummary | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const statusCopy = sidebarStatusCopy(backend);
  const statusDetails = Array.from(
    new Set([statusCopy.secondary, statusLine, statusDetail].filter(Boolean)),
  ) as string[];
  const openConversation = conversations.find((conversation) => conversation.id === openMenuId);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !containerRef.current?.contains(target) &&
        !menuRef.current?.contains(target)
      ) {
        setOpenMenuId(null);
        setMenuPosition(null);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  useEffect(() => {
    if (!openMenuId) {
      return;
    }
    const closeMenu = () => {
      setOpenMenuId(null);
      setMenuPosition(null);
    };

    window.addEventListener("resize", closeMenu);
    window.addEventListener("scroll", closeMenu, true);
    return () => {
      window.removeEventListener("resize", closeMenu);
      window.removeEventListener("scroll", closeMenu, true);
    };
  }, [openMenuId]);

  return (
    <>
      <div
        className={mobileOpen ? "mobile-shell-backdrop is-visible" : "mobile-shell-backdrop"}
        onClick={onCloseMobile}
      />
      <aside
        ref={containerRef}
        className={
          mobileOpen
            ? "nav-sidebar mobile-open transition-[transform,box-shadow] duration-200 ease-out motion-safe:animate-[eagle-panel-left_260ms_ease-out_both]"
            : "nav-sidebar transition-[transform,box-shadow] duration-200 ease-out motion-safe:animate-[eagle-panel-left_260ms_ease-out_both]"
        }
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
                        const shouldOpen = openMenuId !== conversation.id;
                        if (!shouldOpen) {
                          setOpenMenuId(null);
                          setMenuPosition(null);
                          return;
                        }

                        const rect = event.currentTarget.getBoundingClientRect();
                        const menuWidth = 142;
                        const menuHeight = 46;
                        const maxLeft = Math.max(8, window.innerWidth - menuWidth - 8);
                        const left = Math.min(
                          Math.max(8, rect.right - menuWidth),
                          maxLeft,
                        );
                        const top =
                          rect.bottom + 6 + menuHeight <= window.innerHeight - 8
                            ? rect.bottom + 6
                            : Math.max(8, rect.top - menuHeight - 6);

                        setOpenMenuId(conversation.id);
                        setMenuPosition({ left, top });
                      }}
                      type="button"
                    >
                      <MoreHorizontal size={16} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <footer className="nav-sidebar-footer">
          <div className="sidebar-footer-actions">
            <div
              aria-label={`${statusCopy.primary}：${statusDetails.join("；")}`}
              className={`service-status-indicator tone-${statusCopy.tone}`}
              role="status"
              tabIndex={0}
            >
              {backend.phase === "connected" ? <Wifi size={16} /> : <WifiOff size={16} />}
              <div className="service-status-tooltip" role="tooltip">
                <strong>{statusCopy.primary}</strong>
                {statusDetails.map((detail) => (
                  <span key={detail}>{detail}</span>
                ))}
              </div>
            </div>

            <button
              aria-label="打开设置"
              className="secondary-button sidebar-settings-button"
              onClick={() => onOpenSettings("appearance")}
              type="button"
            >
              <Settings2 size={15} />
            </button>
          </div>
        </footer>
      </aside>
      {openConversation && menuPosition && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={menuRef}
              className="floating-menu conversation-floating-menu"
              role="menu"
              style={{ left: menuPosition.left, top: menuPosition.top }}
            >
              <button
                className="floating-menu-item danger"
                onClick={(event) => {
                  event.stopPropagation();
                  setPendingConversationDelete(openConversation);
                  setOpenMenuId(null);
                  setMenuPosition(null);
                }}
                role="menuitem"
                type="button"
              >
                <Trash2 size={14} />
                删除会话
              </button>
            </div>,
            document.body,
          )
        : null}
      <ConfirmationDialog
        description="删除后将移除该会话及其本地记录，无法恢复。"
        onCancel={() => setPendingConversationDelete(null)}
        onConfirm={() => {
          if (pendingConversationDelete) {
            onDeleteConversation(pendingConversationDelete.id);
          }
          setPendingConversationDelete(null);
        }}
        open={pendingConversationDelete !== null}
        title={`删除会话“${pendingConversationDelete?.title || "未命名会话"}”？`}
      />
    </>
  );
}

export const NavigationSidebar = memo(NavigationSidebarComponent);
