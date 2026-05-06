import { useEffect, useMemo, useRef, useState } from "react";
import { ChatWorkspace } from "./components/chat/ChatWorkspace";
import { ActivityInspector } from "./components/inspector/ActivityInspector";
import { AppShell } from "./components/layout/AppShell";
import { NavigationSidebar } from "./components/layout/NavigationSidebar";
import { SettingsDrawer, type SettingsSection } from "./components/settings/SettingsDrawer";

import { useBackendConnection } from "./hooks/useBackendConnection";
import { useTheme } from "./hooks/useTheme";
import {
  deletePersistedConversation,
  loadPersistedConversations,
  loadSettings,
  savePersistedConversation,
  savePersistedConversationIndex,
  type PersistedConversation,
  saveSettings,
} from "./lib/storage";
import type {
  AgentExecutionTrace,
  AppSettings,
  AssistantMessageBlock,
  ChatMessage,
  ConversationSummary,
} from "./types/protocol";

function createConversation(seed?: Partial<ConversationSummary>): ConversationSummary {
  const now = new Date().toISOString();
  return {
    id: seed?.id ?? crypto.randomUUID(),
    title: seed?.title ?? "新对话",
    updatedAt: seed?.updatedAt ?? now,
  };
}

function createExternalConversationSummary(
  conversationId: string,
  seed?: Partial<ConversationSummary>,
): ConversationSummary {
  return {
    id: conversationId,
    title: seed?.title ?? "IM 对话",
    updatedAt: seed?.updatedAt ?? new Date().toISOString(),
  };
}

function createFallbackConversationStore(): PersistedConversation[] {
  return [
    {
      summary: createConversation(),
      messages: [],
    },
  ];
}

function collectMessageTraces(message: ChatMessage) {
  const blockTraces =
    message.blocks?.flatMap((block: AssistantMessageBlock) =>
      block.kind === "trace" ? [block.trace] : [],
    ) ?? [];
  return [...(message.traces ?? []), ...blockTraces];
}

function collectLatestTraces(messages: ChatMessage[]) {
  const traceMap = new Map<string, AgentExecutionTrace>();
  for (const message of messages) {
    for (const trace of collectMessageTraces(message)) {
      traceMap.set(trace.id, trace);
    }
  }

  return Array.from(traceMap.values()).sort(
    (left, right) =>
      new Date(right.completedAt ?? right.startedAt).getTime() -
      new Date(left.completedAt ?? left.startedAt).getTime(),
  );
}

function collectAssetMessages(messages: ChatMessage[]) {
  return messages
    .filter((message) => message.imagePath)
    .map((message) => ({
      id: message.id,
      imagePath: message.imagePath!,
      label: message.label || message.role,
      createdAt: message.createdAt,
    }))
    .reverse();
}

export default function App() {
  const [conversationStore, setConversationStore] = useState<PersistedConversation[]>(
    createFallbackConversationStore,
  );
  const [activeConversationId, setActiveConversationId] = useState(
    () => conversationStore[0].summary.id,
  );
  const [conversationsHydrated, setConversationsHydrated] = useState(false);
  const [settings, setSettings] = useState<AppSettings>(() => loadSettings());
  const [settingsDrawerOpen, setSettingsDrawerOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const saveQueueRef = useRef(Promise.resolve());
  const indexSaveTimerRef = useRef<number | null>(null);
  const conversationSaveTimerRef = useRef<number | null>(null);
  const externalConversationSaveTimerRef = useRef<number | null>(null);
  const pendingConversationSaveIdsRef = useRef<Set<string>>(new Set());
  const pendingDeletedConversationIdsRef = useRef<Set<string>>(new Set());
  const previousConversationIdsRef = useRef<Set<string>>(
    new Set(conversationStore.map((item) => item.summary.id)),
  );
  const conversations = conversationStore.map((item) => item.summary);
  const activeConversation =
    conversationStore.find((item) => item.summary.id === activeConversationId) ??
    conversationStore[0];

  const enqueueSave = (operation: () => Promise<void>) => {
    saveQueueRef.current = saveQueueRef.current
      .catch(() => undefined)
      .then(operation)
      .catch((err) => {
        console.warn("[storage] save failed:", err);
      });
  };

  useTheme(settings.appearance.themeMode);

  useEffect(() => {
    let cancelled = false;
    void loadPersistedConversations()
      .then((persistedConversations) => {
        if (cancelled) {
          return;
        }
        const nextStore =
          persistedConversations.length > 0
            ? persistedConversations
            : createFallbackConversationStore();
        setConversationStore(nextStore);
        setActiveConversationId((current) =>
          nextStore.some((item) => item.summary.id === current)
            ? current
            : nextStore[0].summary.id,
        );
        previousConversationIdsRef.current = new Set(
          nextStore.map((item) => item.summary.id),
        );
        setConversationsHydrated(true);
      })
      .catch((err) => {
        if (cancelled) {
          return;
        }
        console.warn("[storage] failed to hydrate conversations:", err);
        const fallback = createFallbackConversationStore();
        setConversationStore(fallback);
        setActiveConversationId(fallback[0].summary.id);
        previousConversationIdsRef.current = new Set(
          fallback.map((item) => item.summary.id),
        );
        setConversationsHydrated(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const {
    backend,
    messages,
    canSend,
    sendMessage,
    statusLine,
    statusDetail,
    soloStatus,
    soloStep,
    soloConfirmation,
    toolConfirmation,
    soloDisplays,
    soloTimeline,
    soloLastError,
    soloPlan,
    imStatuses,
    wechatBindStatus,
    canStartSolo,
    startSolo,
    requestSoloDisplays,
    startWechatBind,
    cancelWechatBind,
    unbindWechat,
    pauseSolo,
    resumeSolo,
    stopSolo,
    allowDangerousStep,
    rejectDangerousStep,
    allowToolConfirmation,
    rejectToolConfirmation,
  } = useBackendConnection(
    activeConversationId,
    settings,
    activeConversation?.messages ?? [],
    (conversationId, nextMessages) => {
      setConversationStore((current) =>
        current.map((item) =>
          item.summary.id !== conversationId
            ? item
            : item.messages === nextMessages
              ? item
              : {
                  ...item,
                  summary: {
                    ...item.summary,
                    updatedAt:
                      nextMessages[nextMessages.length - 1]?.createdAt ??
                      item.summary.updatedAt,
                  },
                  messages: nextMessages,
                },
        ),
      );
    },
    (conversationId, summary, updater) => {
      pendingConversationSaveIdsRef.current.add(conversationId);
      setConversationStore((current) => {
        const index = current.findIndex((item) => item.summary.id === conversationId);
        if (index >= 0) {
          return current.map((item, itemIndex) => {
            if (itemIndex !== index) {
              return item;
            }
            const nextMessages = updater(item.messages);
            return {
              ...item,
              summary: {
                ...item.summary,
                ...(summary ?? {}),
                id: conversationId,
                updatedAt:
                  nextMessages[nextMessages.length - 1]?.createdAt ??
                  item.summary.updatedAt,
              },
              messages: nextMessages,
            };
          });
        }

        const baseSummary = createExternalConversationSummary(conversationId, summary);
        const messages = updater([]);
        return [
          {
            summary: {
              ...baseSummary,
              updatedAt: messages[messages.length - 1]?.createdAt ?? baseSummary.updatedAt,
            },
            messages,
          },
          ...current,
        ];
      });
    },
  );

  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  useEffect(() => {
    if (!wechatBindStatus) {
      return;
    }
    if (wechatBindStatus.state === "bound" && wechatBindStatus.accountId) {
      setSettings((current) =>
        current.wechat.accountId === wechatBindStatus.accountId
          ? current
          : {
              ...current,
              wechat: {
                ...current.wechat,
                accountId: wechatBindStatus.accountId ?? "",
              },
            },
      );
    }
    if (wechatBindStatus.state === "unbound") {
      setSettings((current) =>
        current.wechat.accountId
          ? {
              ...current,
              wechat: {
                ...current.wechat,
                accountId: "",
                enabled: false,
              },
            }
          : current,
      );
    }
  }, [wechatBindStatus]);

  useEffect(() => {
    return () => {
      if (indexSaveTimerRef.current) {
        window.clearTimeout(indexSaveTimerRef.current);
      }
      if (conversationSaveTimerRef.current) {
        window.clearTimeout(conversationSaveTimerRef.current);
      }
      if (externalConversationSaveTimerRef.current) {
        window.clearTimeout(externalConversationSaveTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!conversationsHydrated) {
      return;
    }

    const summaries = conversationStore.map((item) => item.summary);
    const currentIds = new Set(summaries.map((summary) => summary.id));
    const previousIds = previousConversationIdsRef.current;
    for (const id of previousIds) {
      if (!currentIds.has(id)) {
        pendingDeletedConversationIdsRef.current.add(id);
      }
    }
    for (const id of currentIds) {
      pendingDeletedConversationIdsRef.current.delete(id);
    }
    previousConversationIdsRef.current = currentIds;

    if (indexSaveTimerRef.current) {
      window.clearTimeout(indexSaveTimerRef.current);
    }
    indexSaveTimerRef.current = window.setTimeout(() => {
      const deletedIds = Array.from(pendingDeletedConversationIdsRef.current);
      pendingDeletedConversationIdsRef.current.clear();
      enqueueSave(async () => {
        for (const id of deletedIds) {
          await deletePersistedConversation(id);
        }
        await savePersistedConversationIndex(summaries);
      });
    }, 500);
  }, [conversationStore, conversationsHydrated]);

  useEffect(() => {
    if (!conversationsHydrated || !activeConversation) {
      return;
    }

    if (conversationSaveTimerRef.current) {
      window.clearTimeout(conversationSaveTimerRef.current);
    }
    const conversationToSave = activeConversation;
    conversationSaveTimerRef.current = window.setTimeout(() => {
      enqueueSave(() => savePersistedConversation(conversationToSave));
    }, 500);
  }, [activeConversation, conversationsHydrated]);

  useEffect(() => {
    if (!conversationsHydrated || pendingConversationSaveIdsRef.current.size === 0) {
      return;
    }

    const pendingIds = new Set(pendingConversationSaveIdsRef.current);
    pendingConversationSaveIdsRef.current.clear();
    const conversationsToSave = conversationStore.filter((item) =>
      pendingIds.has(item.summary.id),
    );
    if (conversationsToSave.length === 0) {
      return;
    }

    if (externalConversationSaveTimerRef.current) {
      window.clearTimeout(externalConversationSaveTimerRef.current);
    }
    externalConversationSaveTimerRef.current = window.setTimeout(() => {
      enqueueSave(async () => {
        for (const conversation of conversationsToSave) {
          await savePersistedConversation(conversation);
        }
      });
    }, 500);
  }, [conversationStore, conversationsHydrated]);

  useEffect(() => {
    if (conversationStore.some((item) => item.summary.id === activeConversationId)) {
      return;
    }

    const fallback =
      conversationStore[0]?.summary.id ??
      createConversation({ title: "对话 1" }).id;
    setActiveConversationId(fallback);
  }, [activeConversationId, conversationStore]);

  const traces = useMemo(() => collectLatestTraces(messages), [messages]);
  const assets = useMemo(() => collectAssetMessages(messages), [messages]);

  const createNewConversation = () => {
    const next = createConversation({
      title: `对话 ${conversations.length + 1}`,
    });
    setConversationStore((current) => [
      {
        summary: next,
        messages: [],
      },
      ...current,
    ]);
    setActiveConversationId(next.id);
    setMobileSidebarOpen(false);
  };

  const deleteConversation = (conversationId: string) => {
    setConversationStore((current) => {
      const remaining = current.filter((item) => item.summary.id !== conversationId);
      if (remaining.length > 0) {
        if (conversationId === activeConversationId) {
          setActiveConversationId(remaining[0].summary.id);
        }
        return remaining;
      }

      const replacement = {
        summary: createConversation({ title: "对话 1" }),
        messages: [],
      };
      setActiveConversationId(replacement.summary.id);
      return [replacement];
    });
  };

  return (
    <>
      <AppShell
        inspectorCollapsed={inspectorCollapsed}
        inspectorPanel={
          <ActivityInspector
            assets={assets}
            inspectorCollapsed={inspectorCollapsed}
            onAllowDangerousStep={allowDangerousStep}
            onAllowToolConfirmation={allowToolConfirmation}
            onRejectDangerousStep={rejectDangerousStep}
            onRejectToolConfirmation={rejectToolConfirmation}
            onToggleCollapsed={() => setInspectorCollapsed((current) => !current)}
            soloConfirmation={soloConfirmation}
            soloLastError={soloLastError}
            soloStatus={soloStatus}
            soloTimeline={soloTimeline}
            soloPlan={soloPlan}
            toolConfirmation={toolConfirmation}
            traces={traces}
          />
        }
        mainPanel={
          <ChatWorkspace
            canSend={canSend}
            canStartSolo={canStartSolo}
            messages={messages}
            onAllowDangerousStep={allowDangerousStep}
            onAllowToolConfirmation={allowToolConfirmation}
            onOpenMobileSidebar={() => setMobileSidebarOpen(true)}
            onPermissionModeChange={(mode) =>
              setSettings((current) => ({
                ...current,
                permissions: {
                  ...current.permissions,
                  mode,
                },
              }))
            }
            onRejectDangerousStep={rejectDangerousStep}
            onRejectToolConfirmation={rejectToolConfirmation}
            onSend={sendMessage}
            onSoloPause={pauseSolo}
            onSoloResume={resumeSolo}
            onSoloStart={startSolo}
            onSoloStop={stopSolo}
            settings={settings}
            soloConfirmation={soloConfirmation}
            soloLastError={soloLastError}
            soloStatus={soloStatus}
            toolConfirmation={toolConfirmation}
          />
        }
        sidebarPanel={
          <NavigationSidebar
            activeConversationId={activeConversationId}
            backend={backend}
            conversations={conversations}
            mobileOpen={mobileSidebarOpen}
            onCloseMobile={() => setMobileSidebarOpen(false)}
            onDeleteConversation={deleteConversation}
            onNewConversation={createNewConversation}
            onOpenSettings={(section) => {
              setSettingsSection(section);
              setSettingsDrawerOpen(true);
            }}
            onSelectConversation={(id) => {
              setActiveConversationId(id);
              setMobileSidebarOpen(false);
            }}
            statusDetail={statusDetail}
            statusLine={statusLine}
          />
        }
      />

      <SettingsDrawer
        activeSection={settingsSection}
        imStatuses={imStatuses}
        onCancelWechatBind={cancelWechatBind}
        onChange={setSettings}
        onClose={() => setSettingsDrawerOpen(false)}
        onRefreshSoloDisplays={requestSoloDisplays}
        onSectionChange={setSettingsSection}
        onStartWechatBind={startWechatBind}
        onUnbindWechat={unbindWechat}
        open={settingsDrawerOpen}
        settings={settings}
        soloDisplays={soloDisplays}
        wechatBindStatus={wechatBindStatus}
      />
    </>
  );
}
