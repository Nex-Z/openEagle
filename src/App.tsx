import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { onCloseRequested } from "./lib/electron-bridge";
import { ChatWorkspace } from "./components/chat/ChatWorkspace";
import { ActivityInspector } from "./components/inspector/ActivityInspector";
import { AppShell } from "./components/layout/AppShell";
import { NavigationSidebar } from "./components/layout/NavigationSidebar";
import { SettingsDrawer, type SettingsSection } from "./components/settings/SettingsDrawer";

import { useBackendConnection } from "./hooks/useBackendConnection";
import { useTheme } from "./hooks/useTheme";
import {
  deletePersistedConversation,
  loadActiveConversationId,
  loadPersistedConversations,
  loadSettings,
  loadSettingsFromFile,
  saveActiveConversationId,
  savePersistedConversation,
  savePersistedConversationIndex,
  savePersistedConversations,
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

function isElectronRuntime() {
  return typeof window !== "undefined" && "electronAPI" in window;
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

  // Hydrate settings from Electron file on mount
  useEffect(() => {
    let cancelled = false;
    void loadSettingsFromFile().then((fileSettings) => {
      if (cancelled || !fileSettings) return;
      setSettings((current) => {
        // Only update if file has non-default values that current doesn't have
        const fileKeys = Object.keys(fileSettings) as (keyof AppSettings)[];
        let changed = false;
        const next = { ...current };
        for (const key of fileKeys) {
          const fileVal = JSON.stringify(fileSettings[key]);
          const curVal = JSON.stringify(current[key]);
          if (fileVal !== curVal && fileVal !== JSON.stringify(({} as Record<string, unknown>)[key])) {
            // File has a different (non-empty) value; prefer it
            if (JSON.stringify(current[key]) === JSON.stringify(loadSettings()[key])) {
              // Current is still default, use file value
              (next as Record<string, unknown>)[key] = fileSettings[key];
              changed = true;
            }
          }
        }
        return changed ? next : current;
      });
    });
    return () => { cancelled = true; };
  }, []);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const stored = localStorage.getItem("open-eagle/sidebar-width");
    return stored ? Math.max(180, Math.min(400, Number(stored) || 248)) : 248;
  });
  const [inspectorWidth, setInspectorWidth] = useState(() => {
    const stored = localStorage.getItem("open-eagle/inspector-width");
    return stored ? Math.max(240, Math.min(500, Number(stored) || 318)) : 318;
  });
  const handleSidebarWidthChange = useCallback((width: number) => {
    setSidebarWidth(width);
    localStorage.setItem("open-eagle/sidebar-width", String(width));
  }, []);
  const handleInspectorWidthChange = useCallback((width: number) => {
    setInspectorWidth(width);
    localStorage.setItem("open-eagle/inspector-width", String(width));
  }, []);
  const saveQueueRef = useRef(Promise.resolve());
  const indexSaveTimerRef = useRef<number | null>(null);
  const conversationSaveTimerRef = useRef<number | null>(null);
  const externalConversationSaveTimerRef = useRef<number | null>(null);
  const pendingConversationSaveIdsRef = useRef<Set<string>>(new Set());
  const pendingDeletedConversationIdsRef = useRef<Set<string>>(new Set());
  const conversationStoreRef = useRef(conversationStore);
  const conversationsHydratedRef = useRef(conversationsHydrated);
  const conversations = conversationStore.map((item) => item.summary);
  const activeConversation =
    conversationStore.find((item) => item.summary.id === activeConversationId) ??
    conversationStore[0];
  conversationStoreRef.current = conversationStore;
  conversationsHydratedRef.current = conversationsHydrated;

  const enqueueSave = (operation: () => Promise<void>) => {
    saveQueueRef.current = saveQueueRef.current
      .catch(() => undefined)
      .then(operation)
      .catch((err) => {
        console.warn("[storage] save failed:", err);
      });
  };

  const clearSaveTimers = useCallback(() => {
    if (indexSaveTimerRef.current) {
      window.clearTimeout(indexSaveTimerRef.current);
      indexSaveTimerRef.current = null;
    }
    if (conversationSaveTimerRef.current) {
      window.clearTimeout(conversationSaveTimerRef.current);
      conversationSaveTimerRef.current = null;
    }
    if (externalConversationSaveTimerRef.current) {
      window.clearTimeout(externalConversationSaveTimerRef.current);
      externalConversationSaveTimerRef.current = null;
    }
  }, []);

  const flushPersistedConversations = useCallback(async () => {
    if (!conversationsHydratedRef.current) {
      return;
    }
    clearSaveTimers();
    const deletedIds = Array.from(pendingDeletedConversationIdsRef.current);
    const deletedIdSet = new Set(deletedIds);
    const store = conversationStoreRef.current.filter(
      (conversation) => !deletedIdSet.has(conversation.summary.id),
    );
    pendingDeletedConversationIdsRef.current.clear();
    await saveQueueRef.current.catch(() => undefined);
    for (const id of deletedIds) {
      await deletePersistedConversation(id);
    }
    await savePersistedConversations(store);
  }, [clearSaveTimers]);

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
        const savedActiveConversationId = loadActiveConversationId();
        setConversationStore(nextStore);
        setActiveConversationId((current) => {
          if (
            savedActiveConversationId &&
            nextStore.some((item) => item.summary.id === savedActiveConversationId)
          ) {
            return savedActiveConversationId;
          }
          return nextStore.some((item) => item.summary.id === current)
            ? current
            : nextStore[0].summary.id;
        });
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
    requestSoloDisplays,
    refreshSettings,
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
    scheduledTasks,
    scheduledTaskHistory,
    memoryState,
    requestMemoryState,
    saveMemoryState,
    requestScheduledTasks,
    createScheduledTask,
    updateScheduledTask,
    deleteScheduledTask,
    requestScheduledTaskHistory,
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
    (loadedSettings) => {
      // Backend sent persisted settings; merge with current state
      setSettings((current) => {
        const merged = { ...current, ...loadedSettings };
        saveSettings(merged);
        return merged;
      });
    },
  );

  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  useEffect(() => {
    if (
      conversationsHydrated &&
      conversationStore.some((item) => item.summary.id === activeConversationId)
    ) {
      saveActiveConversationId(activeConversationId);
    }
  }, [activeConversationId, conversationStore, conversationsHydrated]);

  useEffect(() => {
    const flush = () => {
      void flushPersistedConversations();
    };
    const flushWhenHidden = () => {
      if (document.visibilityState === "hidden") {
        flush();
      }
    };

    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", flushWhenHidden);

    let unlistenClose: (() => void) | undefined;
    if (isElectronRuntime()) {
      unlistenClose = onCloseRequested(() => {
        void flushPersistedConversations();
      });
    }

    return () => {
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", flushWhenHidden);
      unlistenClose?.();
    };
  }, [flushPersistedConversations]);

  useEffect(() => {
    if (!wechatBindStatus) {
      return;
    }
    if (wechatBindStatus.state === "bound" && wechatBindStatus.accountId) {
      const accountId = wechatBindStatus.accountId ?? "";
      const boundUserId = wechatBindStatus.userId?.trim();
      setSettings((current) => {
        const alreadyAllowed = boundUserId
          ? current.wechat.allowedUserIds.some((item) => item.trim() === boundUserId)
          : true;
        const allowedUserIds =
          boundUserId && !alreadyAllowed
            ? [...current.wechat.allowedUserIds, boundUserId]
            : current.wechat.allowedUserIds;
        if (
          current.wechat.accountId === accountId &&
          allowedUserIds === current.wechat.allowedUserIds
        ) {
          return current;
        }
        return {
          ...current,
          wechat: {
            ...current.wechat,
            accountId,
            allowedUserIds,
          },
        };
      });
    }
    if (wechatBindStatus.state === "unbound") {
      setSettings((current) =>
        current.wechat.accountId ||
        current.wechat.allowedUserIds.length > 0 ||
        current.wechat.allowedChatIds.length > 0
          ? {
              ...current,
              wechat: {
                ...current.wechat,
                accountId: "",
                enabled: false,
                allowedUserIds: [],
                allowedChatIds: [],
              },
            }
          : current,
      );
    }
  }, [wechatBindStatus]);

  useEffect(() => {
    return clearSaveTimers;
  }, [clearSaveTimers]);

  useEffect(() => {
    if (!conversationsHydrated) {
      return;
    }

    const summaries = conversationStore.map((item) => item.summary);

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
    pendingDeletedConversationIdsRef.current.add(conversationId);
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
        sidebarWidth={sidebarWidth}
        inspectorWidth={inspectorWidth}
        onSidebarWidthChange={handleSidebarWidthChange}
        onInspectorWidthChange={handleInspectorWidthChange}
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
            soloStep={soloStep}
            soloTimeline={soloTimeline}
            soloPlan={soloPlan}
            toolConfirmation={toolConfirmation}
            traces={traces}
          />
        }
        mainPanel={
          <ChatWorkspace
            canSend={canSend}
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
        memoryState={memoryState}
        scheduledTasks={scheduledTasks}
        scheduledTaskHistory={scheduledTaskHistory}
        onCancelWechatBind={cancelWechatBind}
        onChange={setSettings}
        onClose={() => setSettingsDrawerOpen(false)}
        onRefreshSoloDisplays={requestSoloDisplays}
        onRefreshSettings={refreshSettings}
        onRequestMemoryState={requestMemoryState}
        onSectionChange={setSettingsSection}
        onStartWechatBind={startWechatBind}
        onUnbindWechat={unbindWechat}
        onRequestScheduledTasks={requestScheduledTasks}
        onCreateScheduledTask={createScheduledTask}
        onUpdateScheduledTask={updateScheduledTask}
        onDeleteScheduledTask={deleteScheduledTask}
        onRequestScheduledTaskHistory={requestScheduledTaskHistory}
        onSaveMemoryState={saveMemoryState}
        open={settingsDrawerOpen}
        settings={settings}
        soloDisplays={soloDisplays}
        wechatBindStatus={wechatBindStatus}
      />
    </>
  );
}
