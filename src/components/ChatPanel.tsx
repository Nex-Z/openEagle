import { useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { invoke } from "@tauri-apps/api/core";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentExecutionTrace,
  AssistantMessageBlock,
  AppSettings,
  BackendState,
  ChatMessage,
  SoloConfirmationPayload,
  SoloStatusPayload,
  SoloStepPayload,
} from "../types/protocol";

interface ChatPanelProps {
  backend: BackendState;
  messages: ChatMessage[];
  canSend: boolean;
  canStartSolo: boolean;
  onSend: (content: string) => void;
  onSoloStart: (content: string) => Promise<boolean>;
  onSoloPause: () => boolean;
  onSoloResume: () => boolean;
  onSoloStop: () => boolean;
  onSoloAllowDangerousStep: () => boolean;
  onSoloRejectDangerousStep: () => boolean;
  settings: AppSettings;
  soloStatus: SoloStatusPayload;
  soloStep: SoloStepPayload | null;
  soloConfirmation: SoloConfirmationPayload | null;
  soloTimeline: string[];
  soloLastError: string | null;
}

type SlashItem = {
  id: string;
  category: "工具" | "MCP" | "Skill";
  label: string;
  sublabel: string;
  value: string;
  keywords: string[];
};

function buildSlashItems(settings: AppSettings): SlashItem[] {
  return [
    ...settings.tools
      .filter((item) => item.enabled)
      .map((item) => ({
        id: `tool-${item.id}`,
        category: "工具" as const,
        label: item.name,
        sublabel: item.command || item.description || "未配置说明",
        value: `/tool ${item.name} `,
        keywords: [item.name, item.command, item.description].filter(Boolean),
      })),
    ...settings.mcp
      .filter((item) => item.enabled)
      .map((item) => ({
        id: `mcp-${item.id}`,
        category: "MCP" as const,
        label: item.name,
        sublabel: item.endpoint || item.description || "未配置端点",
        value: `/mcp ${item.name} `,
        keywords: [item.name, item.endpoint, item.description, item.transport].filter(
          Boolean,
        ),
      })),
    ...settings.skills
      .filter((item) => item.enabled)
      .map((item) => ({
        id: `skill-${item.id}`,
        category: "Skill" as const,
        label: item.name,
        sublabel: item.description || item.prompt || "未配置说明",
        value: `/skill ${item.name} `,
        keywords: [item.name, item.description, item.prompt].filter(Boolean),
      })),
  ];
}

function findSlashQuery(draft: string, caretIndex: number) {
  const textBeforeCaret = draft.slice(0, caretIndex);
  const slashIndex = textBeforeCaret.lastIndexOf("/");
  if (slashIndex === -1) {
    return null;
  }

  const prefixChar = slashIndex === 0 ? "" : textBeforeCaret[slashIndex - 1];
  if (prefixChar && !/\s/.test(prefixChar)) {
    return null;
  }

  const queryText = textBeforeCaret.slice(slashIndex + 1);
  if (queryText.includes("\n")) {
    return null;
  }

  return {
    slashIndex,
    caretIndex,
    queryText,
  };
}

function isNearBottom(element: HTMLDivElement, threshold = 48) {
  const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
  return remaining <= threshold;
}

function formatTraceValue(value: unknown) {
  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatTraceDuration(startedAt: string, completedAt?: string) {
  if (!completedAt) {
    return "进行中";
  }

  const duration = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(duration) || duration < 0) {
    return "刚刚";
  }
  if (duration < 1000) {
    return `${duration}ms`;
  }

  return `${(duration / 1000).toFixed(duration >= 10_000 ? 0 : 1)}s`;
}

function renderMessageMarkdown(content: string) {
  return (
    <div className="message-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} rel="noopener noreferrer nofollow" target="_blank" />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
function traceKeyFromMessage(message: ChatMessage, trace: AgentExecutionTrace) {
  return `${message.id}:${trace.id}`;
}

function renderTraceItem(
  message: ChatMessage,
  trace: AgentExecutionTrace,
  expandedTraceIds: Set<string>,
  toggleTrace: (traceKey: string) => void,
) {
  const traceKey = traceKeyFromMessage(message, trace);
  const isExpanded = expandedTraceIds.has(traceKey);

  return (
    <div
      key={trace.id}
      className={`trace-item trace-item-compact ${isExpanded ? "is-expanded" : ""}`}
      onClick={() => toggleTrace(traceKey)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleTrace(traceKey);
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="trace-item-summary">
        <span className={`trace-kind trace-kind-${trace.kind}`}>
          {trace.kind.toUpperCase()}
        </span>
        <span className="trace-name">{trace.name}</span>
        <span className="trace-duration">
          {formatTraceDuration(trace.startedAt, trace.completedAt)}
        </span>
        <span className={`trace-status trace-status-${trace.status}`}>
          {trace.status === "started"
            ? "执行中"
            : trace.status === "completed"
              ? "已完成"
              : "失败"}
        </span>
      </div>
      {isExpanded && (trace.params || trace.result) ? (
        <div className="trace-item-body trace-item-body-compact">
          {trace.params ? (
            <div className="trace-section">
              <strong>入参</strong>
              <pre>{formatTraceValue(trace.params)}</pre>
            </div>
          ) : null}
          {trace.result ? (
            <div className="trace-section">
              <strong>执行结果</strong>
              <pre>{formatTraceValue(trace.result)}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function ChatPanel(props: ChatPanelProps) {
  const {
    backend,
    messages,
    canSend,
    canStartSolo,
    onSend,
    onSoloStart,
    onSoloPause,
    onSoloResume,
    onSoloStop,
    onSoloAllowDangerousStep,
    onSoloRejectDangerousStep,
    settings,
    soloStatus,
    soloConfirmation,
  } = props;
  const [draft, setDraft] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<Set<string>>(new Set());
  const [composerMode, setComposerMode] = useState<"chat" | "solo">("chat");
  const [imageDataUrls, setImageDataUrls] = useState<Record<string, string>>({});
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);

  const slashItems = useMemo(() => buildSlashItems(settings), [settings]);
  const caretIndex = textareaRef.current?.selectionStart ?? draft.length;
  const slashQuery = findSlashQuery(draft, caretIndex);
  const normalizedQuery = slashQuery?.queryText.trim().toLowerCase() ?? "";

  const filteredSlashItems = useMemo(() => {
    if (!slashQuery) {
      return [];
    }

    if (!normalizedQuery) {
      return slashItems;
    }

    return slashItems.filter((item) =>
      [item.category, item.label, item.sublabel, ...item.keywords].some((field) =>
        field.toLowerCase().includes(normalizedQuery),
      ),
    );
  }, [normalizedQuery, slashItems, slashQuery]);

  const groupedItems = useMemo(() => {
    const order: SlashItem["category"][] = ["工具", "MCP", "Skill"];
    return order
      .map((category) => ({
        category,
        items: filteredSlashItems.filter((item) => item.category === category),
      }))
      .filter((group) => group.items.length > 0);
  }, [filteredSlashItems]);

  const flatItems = groupedItems.flatMap((group) => group.items);

  const toggleTrace = (traceKey: string) => {
    setExpandedTraceIds((current) => {
      const next = new Set(current);
      if (next.has(traceKey)) {
        next.delete(traceKey);
      } else {
        next.add(traceKey);
      }
      return next;
    });
  };

  useEffect(() => {
    const imagePaths = Array.from(
      new Set(messages.map((message) => message.imagePath).filter(Boolean) as string[]),
    );
    const missing = imagePaths.filter((path) => !imageDataUrls[path]);
    if (missing.length === 0) {
      return;
    }
    let cancelled = false;
    void Promise.all(
      missing.map(async (path) => {
        try {
          const dataUrl = await invoke<string>("read_image_data_url", { path });
          return { path, dataUrl };
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) {
        return;
      }
      setImageDataUrls((current) => {
        const next = { ...current };
        for (const entry of entries) {
          if (!entry) {
            continue;
          }
          next[entry.path] = entry.dataUrl;
        }
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [imageDataUrls, messages]);

  useEffect(() => {
    const stream = streamRef.current;
    if (!stream) {
      return;
    }

    const handleScroll = () => {
      shouldStickToBottomRef.current = isNearBottom(stream);
    };

    handleScroll();
    stream.addEventListener("scroll", handleScroll);
    return () => {
      stream.removeEventListener("scroll", handleScroll);
    };
  }, []);

  useEffect(() => {
    const stream = streamRef.current;
    const latestMessage = messages[messages.length - 1];
    const forceScrollForSolo = latestMessage?.mode === "solo";
    if (!stream || (!shouldStickToBottomRef.current && !forceScrollForSolo)) {
      return;
    }
    if (forceScrollForSolo) {
      shouldStickToBottomRef.current = true;
    }

    requestAnimationFrame(() => {
      stream.scrollTo({
        top: stream.scrollHeight,
        behavior: "auto",
      });
    });
  }, [messages, soloStatus.state]);

  const submit = async () => {
    const normalized = draft.trim();
    if (!normalized) {
      return;
    }

    if (composerMode === "solo") {
      const ok = await onSoloStart(normalized);
      if (ok) {
        setDraft("");
        setActiveIndex(0);
      }
      return;
    }

    onSend(normalized);
    setDraft("");
    setActiveIndex(0);
  };

  const applySlashItem = (item: SlashItem) => {
    if (!slashQuery) {
      return;
    }

    const nextDraft =
      draft.slice(0, slashQuery.slashIndex) +
      item.value +
      draft.slice(slashQuery.caretIndex);

    setDraft(nextDraft);
    setActiveIndex(0);

    requestAnimationFrame(() => {
      if (!textareaRef.current) {
        return;
      }

      const nextCaret = slashQuery.slashIndex + item.value.length;
      textareaRef.current.focus();
      textareaRef.current.setSelectionRange(nextCaret, nextCaret);
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashQuery && flatItems.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((current) => (current + 1) % flatItems.length);
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((current) => (current - 1 + flatItems.length) % flatItems.length);
        return;
      }

      if (event.key === "Enter" && !event.altKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        applySlashItem(flatItems[activeIndex] ?? flatItems[0]);
        return;
      }

      if (event.key === "Escape") {
        event.preventDefault();
        setDraft((current) => {
          if (!slashQuery) {
            return current;
          }
          return (
            current.slice(0, slashQuery.slashIndex) +
            current.slice(slashQuery.caretIndex)
          );
        });
        setActiveIndex(0);
        return;
      }
    }

    if (event.key !== "Enter" || event.nativeEvent.isComposing) {
      return;
    }

    if (event.altKey) {
      return;
    }

    event.preventDefault();
    void submit();
  };

  const isSoloBusy =
    soloStatus.state === "running" ||
    soloStatus.state === "paused" ||
    soloStatus.state === "waiting_user_confirmation";

  return (
    <section className="chat-panel">
      <header className="panel-header">
        <div>
          <p className="eyebrow">桌面智能体</p>
          <h2>主对话区</h2>
        </div>
        {backend.phase === "connected" ? (
          <span className="toggle-chip">SOLO {soloStatus.state}</span>
        ) : null}
      </header>

      <div ref={streamRef} className="message-stream">
        {messages.length === 0 ? (
          <div className="empty-state">
            <p>已连接后即可在这里发起任务、查看回复和追踪状态。</p>
            <small>输入 `/` 可快速插入 Tool、MCP 和 Skill 指令。</small>
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
                      : message.role === "tool"
                        ? "工具"
                      : "系统"}
                  {message.mode === "solo" ? " · SOLO" : ""}
                </strong>
                <span>{new Date(message.createdAt).toLocaleTimeString()}</span>
              </div>
              {message.label ? <div className="message-label">{message.label}</div> : null}
              {message.blocks && message.blocks.length > 0 ? (
                <div className="assistant-blocks">
                  {message.blocks.map((block: AssistantMessageBlock) =>
                    block.kind === "text" ? (
                      block.content ? (
                        <div key={block.id}>{renderMessageMarkdown(block.content)}</div>
                      ) : null
                    ) : (
                      renderTraceItem(
                        message,
                        block.trace,
                        expandedTraceIds,
                        toggleTrace,
                      )
                    ),
                  )}
                </div>
              ) : message.content ? (
                <div>{renderMessageMarkdown(message.content)}</div>
              ) : null}
              {message.imagePath ? (
                <div className="message-image-wrap">
                  <img
                    alt={message.label || "SOLO screenshot"}
                    className="message-image"
                    src={imageDataUrls[message.imagePath] ?? convertFileSrc(message.imagePath)}
                  />
                  <small>{message.imagePath}</small>
                </div>
              ) : null}
              {(!message.blocks || message.blocks.length === 0) &&
              message.traces &&
              message.traces.length > 0 ? (
                <div className="trace-list-block">
                  <div className="trace-list-title">本轮调用</div>
                  <div className="trace-list">
                    {message.traces.map((trace) =>
                      renderTraceItem(message, trace, expandedTraceIds, toggleTrace),
                    )}
                  </div>
                </div>
              ) : null}
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
        {isSoloBusy ? (
          <div className="solo-status-card solo-status-card-compact">
            <div className="solo-status-row">
              <strong>SOLO 模式已启用</strong>
              <span>
                {soloStatus.stepCount}/{soloStatus.maxSteps}
              </span>
            </div>
            <small>状态: {soloStatus.state}{soloStatus.detail ? ` · ${soloStatus.detail}` : ""}</small>
            <div className="solo-controls">
              {soloStatus.state === "running" ? (
                <button className="secondary-action" onClick={onSoloPause} type="button">
                  暂停
                </button>
              ) : null}
              {soloStatus.state === "paused" ? (
                <button className="secondary-action" onClick={onSoloResume} type="button">
                  继续
                </button>
              ) : null}
              {soloStatus.state === "waiting_user_confirmation" && soloConfirmation ? (
                <button
                  className="secondary-action"
                  onClick={onSoloAllowDangerousStep}
                  type="button"
                >
                  允许本次
                </button>
              ) : null}
              {soloStatus.state === "waiting_user_confirmation" && soloConfirmation ? (
                <button
                  className="secondary-action"
                  onClick={onSoloRejectDangerousStep}
                  type="button"
                >
                  拒绝本次
                </button>
              ) : null}
              <button className="text-action danger" onClick={onSoloStop} type="button">
                结束
              </button>
            </div>
          </div>
        ) : null}

        <div className="composer-field">
          <textarea
            ref={textareaRef}
            className="composer-input"
            disabled={!canSend}
            onChange={(event) => {
              setDraft(event.target.value);
              setActiveIndex(0);
            }}
            onClick={() => setActiveIndex(0)}
            onKeyDown={handleKeyDown}
            placeholder={
              canSend
                ? composerMode === "solo"
                  ? "SOLO: 描述你要自动操作电脑完成的任务..."
                  : "输入任务，或使用 / 调出 Tool / MCP / Skill..."
                : "等待后端启动完成..."
            }
            rows={3}
            value={draft}
          />

          {slashQuery && composerMode === "chat" ? (
            <div className="slash-panel" role="listbox">
              <div className="slash-panel-header">
                <strong>命令面板</strong>
                <span>支持搜索、上下键选择、Enter 确认</span>
              </div>

              {groupedItems.length > 0 ? (
                <div className="slash-group-list">
                  {groupedItems.map((group) => (
                    <div key={group.category} className="slash-group">
                      <div className="slash-group-title">{group.category}</div>
                      {group.items.map((item) => {
                        const itemIndex = flatItems.findIndex((entry) => entry.id === item.id);
                        const isActive = itemIndex === activeIndex;
                        return (
                          <button
                            key={item.id}
                            className={isActive ? "slash-item active" : "slash-item"}
                            onClick={() => applySlashItem(item)}
                            onMouseEnter={() => setActiveIndex(itemIndex)}
                            type="button"
                          >
                            <span className="slash-item-label">{item.label}</span>
                            <span className="slash-item-meta">{item.sublabel}</span>
                          </button>
                        );
                      })}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="slash-empty">没有匹配项，继续输入关键词试试。</div>
              )}
            </div>
          ) : null}

          <div className="composer-bottom">
            <button
              className={composerMode === "solo" ? "solo-mode-btn active" : "solo-mode-btn"}
              disabled={!canStartSolo}
              onClick={() =>
                setComposerMode((current) => (current === "solo" ? "chat" : "solo"))
              }
              title={!canStartSolo ? "请先在设置中配置 VL 模型 ID 和 API Key" : "切换 SOLO"}
              type="button"
            >
              SOLO
            </button>

            <button
              aria-label="发送消息"
              className="send-action"
              disabled={!canSend || !draft.trim() || (composerMode === "solo" && !canStartSolo)}
              onClick={() => {
                void submit();
              }}
              type="button"
            >
              <svg aria-hidden="true" viewBox="0 0 24 24">
                <path d="M3.4 20.4 21 12 3.4 3.6l.1 6.1 10.6 2.3-10.6 2.3-.1 6.1Z" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
