import fs from "node:fs";
import path from "node:path";
import { app } from "electron";

const CONVERSATION_INDEX_FILE = "conversation-index.json";
const CONVERSATION_DIR = "conversations";
const DELETED_CONVERSATION_DIR = "deleted-conversations";

function conversationStoreRoot(): string {
  return app.getPath("userData");
}

function conversationIndexPath(): string {
  return path.join(conversationStoreRoot(), CONVERSATION_INDEX_FILE);
}

function conversationsDir(): string {
  return path.join(conversationStoreRoot(), CONVERSATION_DIR);
}

function deletedConversationsDir(): string {
  return path.join(conversationStoreRoot(), DELETED_CONVERSATION_DIR);
}

function validateConversationId(id: string): boolean {
  if (!id) return false;
  return /^[A-Za-z0-9_-]+$/.test(id);
}

function conversationFilePath(conversationId: string): string {
  if (!validateConversationId(conversationId)) {
    throw new Error("conversationId may only contain A-Z, a-z, 0-9, _ and -");
  }
  return path.join(conversationsDir(), `${conversationId}.json`);
}

function readJsonFile(filePath: string): unknown {
  const text = fs.readFileSync(filePath, "utf-8");
  return JSON.parse(text);
}

function atomicWriteJson(filePath: string, value: unknown): void {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });

  const tmpPath = filePath + ".tmp";
  const data = JSON.stringify(value, null, 2) + "\n";
  // Open with 'w' mode so fsync succeeds on Windows
  const fd = fs.openSync(tmpPath, "w");
  try {
    fs.writeSync(fd, data);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  if (fs.existsSync(filePath)) {
    fs.unlinkSync(filePath);
  }
  fs.renameSync(tmpPath, filePath);
}

function normalizeIndex(value: unknown): Record<string, unknown> {
  const obj = value as Record<string, unknown> | undefined;
  return {
    version: obj?.version ?? 1,
    conversations: Array.isArray(obj?.conversations) ? obj.conversations : [],
  };
}

function normalizeConversationFile(value: unknown): Record<string, unknown> {
  const obj = value as Record<string, unknown>;
  const summary = obj?.summary as Record<string, unknown> | undefined;
  if (!summary || typeof summary !== "object") {
    throw new Error("conversation file requires summary object");
  }
  const conversationId = summary.id;
  if (typeof conversationId !== "string" || !validateConversationId(conversationId)) {
    throw new Error("conversation summary requires valid id");
  }
  return {
    version: obj.version ?? 1,
    summary,
    messages: Array.isArray(obj.messages) ? obj.messages : [],
    savedAt: typeof obj.savedAt === "string" ? obj.savedAt : new Date().toISOString(),
  };
}

export function loadConversationIndex(): unknown {
  const filePath = conversationIndexPath();
  if (!fs.existsSync(filePath)) {
    return { version: 1, conversations: [] };
  }
  return normalizeIndex(readJsonFile(filePath));
}

export function saveConversationIndex(index: unknown): { ok: boolean } {
  const normalized = normalizeIndex(index);
  atomicWriteJson(conversationIndexPath(), normalized);
  return { ok: true };
}

export function loadConversationFile(conversationId: string): unknown {
  const filePath = conversationFilePath(conversationId);
  if (!fs.existsSync(filePath)) {
    throw new Error(`conversation file does not exist: ${conversationId}`);
  }
  return normalizeConversationFile(readJsonFile(filePath));
}

export function saveConversationFile(conversation: unknown): { ok: boolean } {
  const normalized = normalizeConversationFile(conversation);
  const conversationId = (normalized.summary as Record<string, unknown>).id as string;
  const filePath = conversationFilePath(conversationId);
  atomicWriteJson(filePath, normalized);
  return { ok: true };
}

export function deleteConversationFile(conversationId: string): { ok: boolean } {
  const filePath = conversationFilePath(conversationId);
  if (fs.existsSync(filePath)) {
    const deletedDir = deletedConversationsDir();
    fs.mkdirSync(deletedDir, { recursive: true });
    const deletedPath = path.join(
      deletedDir,
      `${conversationId}-${Date.now()}.json`
    );
    fs.renameSync(filePath, deletedPath);
  }
  return { ok: true };
}

export function loadSoloRunLog(requestId: string): unknown {
  if (!validateConversationId(requestId)) {
    throw new Error("requestId may only contain A-Z, a-z, 0-9, _ and -");
  }
  const fileName = `${requestId}.jsonl`;
  const candidates = [
    path.join(projectRoot(), ".open-eagle", "solo-runs", fileName),
    path.join(conversationStoreRoot(), ".open-eagle", "solo-runs", fileName),
  ];

  const filePath = candidates.find((p) => fs.existsSync(p));
  if (!filePath) {
    return { requestId, records: [] };
  }

  const text = fs.readFileSync(filePath, "utf-8");
  const records: unknown[] = [];
  let parseErrors = 0;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      records.push(JSON.parse(trimmed));
    } catch {
      parseErrors++;
    }
  }

  return { requestId, path: filePath, records, parseErrors };
}

function projectRoot(): string {
  return path.resolve(__dirname, "..");
}

// --- Settings file persistence ---

const SETTINGS_FILE = "settings.json";

function settingsFilePath(): string {
  return path.join(conversationStoreRoot(), SETTINGS_FILE);
}

export function loadAppSettings(): unknown {
  const filePath = settingsFilePath();
  if (!fs.existsSync(filePath)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return null;
  }
}

export function saveAppSettings(settings: unknown): { ok: boolean } {
  atomicWriteJson(settingsFilePath(), settings);
  return { ok: true };
}
