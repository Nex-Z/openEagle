import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Camera,
  Clipboard,
  Copy,
  ExternalLink,
  LoaderCircle,
  MessageSquareText,
  SendHorizonal,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { emit, invoke, listen } from "../../lib/electron-bridge";
import type {
  AttachmentRef,
  QuickAssistantRuntimeState,
  QuickAssistantSubmitPayload,
  QuickContextItem,
} from "../../types/protocol";

interface QuickContextPayload {
  selectionText?: string;
  capturedAt?: string;
  reset?: boolean;
}

interface ScreenshotCaptureResult {
  ok: boolean;
  cancelled?: boolean;
  error?: string;
  attachment?: AttachmentRef;
}

const ACTIONS = [
  {
    id: "summarize",
    label: "总结",
    prompt: "请用简洁要点总结当前上下文。",
  },
  {
    id: "explain",
    label: "解释",
    prompt: "请解释当前上下文，优先说清核心概念和隐含前提。",
  },
  {
    id: "criticize",
    label: "找问题",
    prompt: "请指出当前上下文中值得怀疑、缺证据或可能有偏差的地方。",
  },
  {
    id: "translate",
    label: "翻译",
    prompt: "请把当前上下文翻译成中文，保留关键术语。",
  },
  {
    id: "notes",
    label: "笔记",
    prompt: "请把当前上下文整理成结构化阅读笔记。",
  },
] as const;

function createId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function normalizeText(text: string) {
  return text.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

function contextTitleForSelection(text: string) {
  return `选中 ${text.length} 字`;
}

function statusLabel(state: QuickAssistantRuntimeState) {
  if (!state.backendReady) {
    return "后端未就绪";
  }
  switch (state.status) {
    case "pending":
      return "思考中";
    case "done":
      return "已完成";
    case "error":
      return "失败";
    case "solo":
      return "桌面执行";
    default:
      return "可发送";
  }
}

function buildContextText(items: QuickContextItem[]) {
  const blocks = items.map((item) => {
    if (item.kind === "selection" && item.content) {
      return `【选中文字】\n${item.content}`;
    }
    if (item.kind === "screenshot") {
      return `【截图附件】${item.title}（图片已随消息附带，请直接阅读图片内容）`;
    }
    if (item.content) {
      return `【上下文】${item.content}`;
    }
    return `【上下文】${item.title}`;
  });
  return blocks.join("\n\n");
}

function screenshotInstruction(count: number) {
  const target = count > 1 ? "这些截图" : "这张截图";
  return [
    `请直接阅读随本消息附带的${target}，不要只回复“我看看”或确认收到。`,
    "先说明截图里主要是什么，再根据下面的动作给出具体结果。",
    "如果截图中文字较小或局部无法辨认，请明确指出不确定的位置。",
  ].join("\n");
}

function resetResponseState(current: QuickAssistantRuntimeState, detail = ""): QuickAssistantRuntimeState {
  return {
    ...current,
    status: "idle",
    content: "",
    detail,
    quickRequestId: undefined,
    requestId: undefined,
    attachments: undefined,
  };
}

function formatAttachmentMeta(attachment: AttachmentRef) {
  if (!attachment.size) {
    return "截图";
  }
  if (attachment.size < 1024 * 1024) {
    return `${(attachment.size / 1024).toFixed(1)} KB`;
  }
  return `${(attachment.size / (1024 * 1024)).toFixed(1)} MB`;
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const element = document.createElement("textarea");
    element.value = text;
    element.style.position = "fixed";
    element.style.opacity = "0";
    document.body.appendChild(element);
    element.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(element);
    return ok;
  }
}

export function QuickAssistantWindow() {
  const [contexts, setContexts] = useState<QuickContextItem[]>([]);
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [draft, setDraft] = useState("");
  const [selectedActionId, setSelectedActionId] = useState<string>("summarize");
  const [responseState, setResponseState] = useState<QuickAssistantRuntimeState>({
    status: "idle",
    backendReady: false,
  });
  const [captureStatus, setCaptureStatus] = useState<string>("");
  const [copied, setCopied] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    void invoke("quick_assistant_ready").catch((err: unknown) =>
      console.error("[QUICK] ready failed:", err),
    );
  }, []);

  useEffect(() => {
    const contextUnlisten = listen<QuickContextPayload>("quick://context", (event) => {
      const payload = event.payload;
      const selectionText = normalizeText(payload.selectionText ?? "");
      if (payload.reset) {
        setDraft("");
        setAttachments([]);
        setResponseState((current) => ({
          ...current,
          status: "idle",
          content: "",
          detail: "",
          quickRequestId: undefined,
          requestId: undefined,
        }));
      }
      setContexts((current) => {
        const withoutSelection = current.filter((item) => item.kind !== "selection");
        if (!selectionText) {
          return payload.reset ? [] : withoutSelection;
        }
        return [
          {
            id: createId("ctx-selection"),
            kind: "selection",
            title: contextTitleForSelection(selectionText),
            content: selectionText,
            createdAt: payload.capturedAt ?? new Date().toISOString(),
          },
          ...(payload.reset ? [] : withoutSelection),
        ];
      });
      requestAnimationFrame(() => textareaRef.current?.focus());
    });

    const stateUnlisten = listen<QuickAssistantRuntimeState>("quick://state", (event) => {
      setResponseState((current) => ({ ...current, ...event.payload }));
    });

    return () => {
      contextUnlisten.then((fn) => fn());
      stateUnlisten.then((fn) => fn());
    };
  }, []);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) {
      return;
    }
    element.style.height = "0px";
    element.style.height = `${Math.min(Math.max(element.scrollHeight, 72), 132)}px`;
  }, [draft]);

  const selectedAction = useMemo(
    () => ACTIONS.find((action) => action.id === selectedActionId) ?? ACTIONS[0],
    [selectedActionId],
  );

  const canSend =
    Boolean(responseState.backendReady) &&
    responseState.status !== "pending" &&
    (Boolean(draft.trim()) || contexts.length > 0 || attachments.length > 0);
  const responseContent = responseState.content?.trim() ?? "";
  const responseDetail = responseState.detail?.trim() ?? responseState.backendDetail?.trim() ?? "";

  const removeContext = useCallback((id: string) => {
    setContexts((current) => current.filter((item) => item.id !== id));
    setAttachments((current) => current.filter((item) => item.id !== id));
    setResponseState((current) => resetResponseState(current));
    setCaptureStatus("");
  }, []);

  const clearAllContext = useCallback(() => {
    setContexts([]);
    setAttachments([]);
    setResponseState((current) => resetResponseState(current));
    setCaptureStatus("");
  }, []);

  const handleCaptureScreenshot = useCallback(async () => {
    if (attachments.length >= 5) {
      setCaptureStatus("单条消息最多 5 个附件。");
      return;
    }
    setCaptureStatus("选择截图区域...");
    const result = await invoke<ScreenshotCaptureResult>("capture_context_screenshot");
    if (!result.ok || !result.attachment) {
      setCaptureStatus(result.cancelled ? "" : result.error ?? "截图失败。");
      return;
    }

    const attachment = result.attachment;
    setAttachments((current) => [...current, attachment]);
    setContexts((current) => [
      ...current,
      {
        id: attachment.id,
        kind: "screenshot",
        title: attachment.name || "截图上下文",
        attachmentId: attachment.id,
        createdAt: new Date().toISOString(),
      },
    ]);
    setResponseState((current) => resetResponseState(current));
    setCaptureStatus("截图已加入，点击发送分析。");
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [attachments.length]);

  const buildSubmitContent = useCallback(() => {
    const userText = normalizeText(draft);
    const contextText = buildContextText(contexts);
    const imageAttachmentCount = attachments.filter((attachment) => attachment.kind === "image").length;
    const parts: string[] = [selectedAction.prompt];
    if (imageAttachmentCount > 0) {
      parts.unshift(screenshotInstruction(imageAttachmentCount));
    }
    if (userText) {
      parts.push(`用户补充：\n${userText}`);
    }
    if (contextText) {
      parts.push(`上下文：\n${contextText}`);
    }
    if (!userText && !contextText && attachments.length > 0) {
      parts.push("请分析这张截图。");
    }
    return parts.join("\n\n").trim();
  }, [attachments.length, contexts, draft, selectedAction.prompt]);

  const handleSubmit = useCallback(() => {
    if (!canSend) {
      return;
    }
    const content = buildSubmitContent();
    if (!content) {
      return;
    }

    const quickRequestId = createId("quick");
    const payload: QuickAssistantSubmitPayload = {
      quickRequestId,
      content,
      actionId: selectedAction.id,
      contextItems: contexts,
      attachments,
      createdAt: new Date().toISOString(),
    };
    emit("quick:submit", payload);
    setCaptureStatus("");
    setResponseState((current) => ({
      ...current,
      quickRequestId,
      status: "pending",
      content: "",
      detail: "请求已发送。",
    }));
    setDraft("");
    setCopied(false);
  }, [attachments, buildSubmitContent, canSend, contexts, selectedAction.id]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.altKey || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    handleSubmit();
  };

  const handleCopy = async () => {
    const text = responseContent || responseDetail;
    if (!text) {
      return;
    }
    const ok = await copyText(text);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    }
  };

  return (
    <section className="quick-assistant-root">
      <div className={`quick-assistant-card status-${responseState.status ?? "idle"}`}>
        <header className="quick-assistant-header quick-assistant-drag-region">
          <div className="quick-assistant-title">
            <Sparkles size={15} />
            <strong>Quick Assistant</strong>
            <span>{statusLabel(responseState)}</span>
          </div>
          <div className="quick-assistant-header-actions">
            <button
              aria-label="打开主窗口"
              className="quick-icon-button"
              onClick={() => emit("quick:open-main")}
              title="打开主窗口"
              type="button"
            >
              <ExternalLink size={15} />
            </button>
            <button
              aria-label="关闭"
              className="quick-icon-button"
              onClick={() => emit("quick:dismiss")}
              title="关闭"
              type="button"
            >
              <X size={15} />
            </button>
          </div>
        </header>

        <main className="quick-assistant-body">
          <div className="quick-context-row">
            {contexts.length > 0 ? (
              contexts.map((item) => (
                <button
                  className={`quick-context-chip kind-${item.kind}`}
                  key={item.id}
                  onClick={() => removeContext(item.id)}
                  title="移除此上下文"
                  type="button"
                >
                  {item.kind === "screenshot" ? <Camera size={13} /> : <Clipboard size={13} />}
                  <span>{item.title}</span>
                  <X size={12} />
                </button>
              ))
            ) : (
              <span className="quick-empty-context">无上下文</span>
            )}
            {contexts.length > 0 ? (
              <button
                aria-label="清空上下文"
                className="quick-context-clear"
                onClick={clearAllContext}
                title="清空上下文"
                type="button"
              >
                <Trash2 size={13} />
              </button>
            ) : null}
          </div>

          {attachments.length > 0 ? (
            <div className="quick-attachment-strip">
              {attachments.map((attachment) => (
                <div className="quick-attachment" key={attachment.id}>
                  {attachment.contentBase64 ? (
                    <img alt={attachment.name || "截图"} src={attachment.contentBase64} />
                  ) : (
                    <Camera size={16} />
                  )}
                  <span>{formatAttachmentMeta(attachment)}</span>
                </div>
              ))}
            </div>
          ) : null}

          {responseState.status && responseState.status !== "idle" ? (
            <section className="quick-response-panel">
              <div className="quick-response-head">
                <span>
                  {responseState.status === "pending" ? (
                    <LoaderCircle className="spin" size={14} />
                  ) : (
                    <MessageSquareText size={14} />
                  )}
                  {responseState.status === "solo" ? "已交给桌面执行" : statusLabel(responseState)}
                </span>
                <button
                  aria-label="复制回复"
                  className="quick-icon-button"
                  disabled={!responseContent && !responseDetail}
                  onClick={handleCopy}
                  title="复制回复"
                  type="button"
                >
                  <Copy size={14} />
                </button>
              </div>
              <div className="quick-response-content">
                {responseContent ? (
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{responseContent}</ReactMarkdown>
                ) : (
                  <p>{responseDetail || "等待回复..."}</p>
                )}
              </div>
              {copied ? <small className="quick-copy-status">已复制</small> : null}
            </section>
          ) : null}

          <div className="quick-actions" aria-label="快捷动作">
            {ACTIONS.map((action) => (
              <button
                className={action.id === selectedActionId ? "is-active" : ""}
                key={action.id}
                onClick={() => setSelectedActionId(action.id)}
                type="button"
              >
                {action.label}
              </button>
            ))}
          </div>

          <div className="quick-composer">
            <textarea
              ref={textareaRef}
              disabled={!responseState.backendReady || responseState.status === "pending"}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={responseState.backendReady ? "问点什么..." : "等待后端连接..."}
              value={draft}
            />
            <div className="quick-composer-footer">
              <span className={captureStatus ? "has-message" : ""}>
                {captureStatus || responseDetail || ""}
              </span>
              <button
                aria-label="截图"
                className="quick-icon-button"
                disabled={responseState.status === "pending"}
                onClick={() => void handleCaptureScreenshot()}
                title="截图"
                type="button"
              >
                <Camera size={15} />
              </button>
              <button
                aria-label="发送"
                className="quick-send-button"
                disabled={!canSend}
                onClick={handleSubmit}
                title="发送"
                type="button"
              >
                <SendHorizonal size={16} />
              </button>
            </div>
          </div>
        </main>
      </div>
    </section>
  );
}
