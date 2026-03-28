import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { ThemeToggle } from "./components/ThemeToggle";
import { useBackendConnection } from "./hooks/useBackendConnection";
import { useTheme } from "./hooks/useTheme";
import {
  loadPersistedConversations,
  loadSettings,
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
  const initialConversations = loadPersistedConversations().map((item) => item.summary);
  const [conversations, setConversations] = useState<ConversationSummary[]>(
    initialConversations.length > 0 ? initialConversations : [createConversation()],
  );
  const [activeConversationId, setActiveConversationId] = useState(
    initialConversations[0]?.id ?? conversations[0].id,
  );
  const [showSettings, setShowSettings] = useState(false);
  const [settings, setSettings] = useState<AppSettings>(loadSettings());

  useTheme(settings.appearance.themeMode);

  const { backend, messages, canSend, sendMessage, statusLine } =
    useBackendConnection(activeConversationId, settings);

  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  useEffect(() => {
    savePersistedConversations(
      conversations.map((summary) => ({
        summary,
        messages: summary.id === activeConversationId ? messages : [],
      })),
    );
  }, [activeConversationId, conversations, messages]);

  const createNewConversation = () => {
    const next = createConversation({
      title: `对话 ${conversations.length + 1}`,
    });
    setConversations((current) => [next, ...current]);
    setActiveConversationId(next.id);
    setShowSettings(false);
  };

  return (
    <main className="app-shell">
      <Sidebar
        activeConversationId={activeConversationId}
        conversations={conversations}
        onNewConversation={createNewConversation}
        onOpenSettings={() => setShowSettings(true)}
        onSelectConversation={(id) => {
          setActiveConversationId(id);
          setShowSettings(false);
        }}
        quickTheme={
          <ThemeToggle
            onChange={(themeMode) =>
              setSettings((current) => ({
                ...current,
                appearance: { themeMode },
              }))
            }
            value={settings.appearance.themeMode}
          />
        }
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
            statusLine={statusLine}
          />
        )}
      </div>
    </main>
  );
}
