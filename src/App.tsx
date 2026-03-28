import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { useBackendConnection } from "./hooks/useBackendConnection";
import { useTheme } from "./hooks/useTheme";
import {
  loadPersistedConversations,
  loadSettings,
  type PersistedConversation,
  savePersistedConversations,
  saveSettings,
} from "./lib/storage";
import type { AppSettings, ConversationSummary } from "./types/protocol";

function createConversation(seed?: Partial<ConversationSummary>): ConversationSummary {
  const now = new Date().toISOString();
  return {
    id: seed?.id ?? crypto.randomUUID(),
    title: seed?.title ?? "新对话",
    updatedAt: seed?.updatedAt ?? now,
  };
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
  const [showSettings, setShowSettings] = useState(false);
  const [settings, setSettings] = useState<AppSettings>(() => loadSettings());
  const conversations = conversationStore.map((item) => item.summary);
  const activeConversation =
    conversationStore.find((item) => item.summary.id === activeConversationId) ??
    conversationStore[0];

  useTheme(settings.appearance.themeMode);

  const { backend, messages, canSend, sendMessage, statusLine, statusDetail } =
    useBackendConnection(
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
    setShowSettings(false);
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
    setShowSettings(false);
  };

  return (
    <main className="app-shell">
      <Sidebar
        activeConversationId={activeConversationId}
        backend={backend}
        conversations={conversations}
        onDeleteConversation={deleteConversation}
        onNewConversation={createNewConversation}
        onOpenSettings={() => setShowSettings(true)}
        onSelectConversation={(id) => {
          setActiveConversationId(id);
          setShowSettings(false);
        }}
        statusDetail={statusDetail}
        statusLine={statusLine}
      />

      <div className="content-panel">
        {showSettings ? (
          <SettingsPanel
            onChange={setSettings}
            onClose={() => setShowSettings(false)}
            settings={settings}
          />
        ) : (
          <ChatPanel
            backend={backend}
            canSend={canSend}
            messages={messages}
            onSend={sendMessage}
          />
        )}
      </div>
    </main>
  );
}
