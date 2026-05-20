import fs from "node:fs";
import path from "node:path";
import { ClassicLevel } from "classic-level";

const TAURI_DATA_DIR = path.join(
  process.env.LOCALAPPDATA || path.join(process.env.HOME || "", "AppData", "Local"),
  "com.openeagle.desktop",
);
const LEVELDB_DIR = path.join(TAURI_DATA_DIR, "EBWebView", "Default", "Local Storage", "leveldb");
const SETTINGS_KEY = "open-eagle/settings";

function swapEndian(buf: Buffer): Buffer {
  const out = Buffer.from(buf);
  for (let i = 0; i < out.length - 1; i += 2) {
    const tmp = out[i]; out[i] = out[i + 1]; out[i + 1] = tmp;
  }
  return out;
}

function tryParseJson(str: string): Record<string, unknown> | null {
  const lastBrace = str.lastIndexOf("}");
  const trimmed = lastBrace >= 0 ? str.slice(0, lastBrace + 1) : str;
  try { return JSON.parse(trimmed) as Record<string, unknown>; } catch { /* */ }
  try { return JSON.parse(trimmed.replace(/\n/g, "\\n").replace(/\r/g, "\\r")) as Record<string, unknown>; } catch { /* */ }
  return null;
}

export async function migrateTauriSettings(): Promise<Record<string, unknown> | null> {
  if (!fs.existsSync(LEVELDB_DIR)) return null;

  let db: ClassicLevel<Buffer, Buffer> | null = null;
  try {
    db = new ClassicLevel<Buffer, Buffer>(LEVELDB_DIR);

    for await (const [keyRaw, valueRaw] of db.iterator()) {
      const key = Buffer.from(keyRaw).toString("utf-8");
      if (!key.includes(SETTINGS_KEY)) continue;

      const raw = Buffer.from(valueRaw);

      // Try UTF-16BE (swap byte pairs)
      const swapped = swapEndian(raw);
      const result = tryParseJson(swapped.toString("utf-16le"));
      if (result) {
        console.log("[MIGRATE] Settings imported from Tauri");
        return result;
      }

      console.log("[MIGRATE] Tauri settings found but could not be parsed (data may be corrupted)");
      return null;
    }
    return null;
  } catch {
    return null;
  } finally {
    if (db) { try { await db.close(); } catch { /* */ } }
  }
}
