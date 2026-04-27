import { useEffect, useMemo, useState } from "react";
import { ChatWorkspace } from "./components/chat/ChatWorkspace";
import { ActivityInspector } from "./components/inspector/ActivityInspector";
import { AppShell } from "./components/layout/AppShell";
import { NavigationSidebar } from "./components/layout/NavigationSidebar";
import { SettingsDrawer, type SettingsSection } from "./components/settings/SettingsDrawer";
import { useBackendConnection } from "./hooks/useBackendConnection";
import { useTheme } from "./hooks/useTheme";
import {
  loadPersistedConversations,
  loadSettings,
  type PersistedConversation,
  savePersistedConversations,
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
  const [conversationStore, setConversationStore] = useState<PersistedConversation[]>(() => {
    const persistedConversations = loadPersistedConversations();
    return persistedConversations.length > 0
      ? persistedConversations
      : [
          {
            summary: createConversation(),
            messages: [],
          },
        ];
  });
  const [activeConversationId, setActiveConversationId] = useState(
    () => conversationStore[0].summary.id,
  );
  const [settings, setSettings] = useState<AppSettings>(() => loadSettings());
  const [settingsDrawerOpen, setSettingsDrawerOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const conversations = conversationStore.map((item) => item.summary);
  const activeConversation =
    conversationStore.find((item) => item.summary.id === activeConversationId) ??
    conversationStore[0];

  useTheme(settings.appearance.themeMode);

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
    canStartSolo,
    startSolo,
    requestSoloDisplays,
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
  );

  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  useEffect(() => {
    savePersistedConversations(conversationStore);
  }, [conversationStore]);

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
            soloStep={soloStep}
            soloTimeline={soloTimeline}
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
        onChange={setSettings}
        onClose={() => setSettingsDrawerOpen(false)}
        onRefreshSoloDisplays={requestSoloDisplays}
        onSectionChange={setSettingsSection}
        open={settingsDrawerOpen}
        settings={settings}
        soloDisplays={soloDisplays}
      />
    </>
  );
}
