import { mouse, keyboard, Button, Key, Point } from "@nut-tree-fork/nut-js";

interface ActionPayload {
  action: string;
  x?: number;
  y?: number;
  delta?: number;
  text?: string;
  keys?: string[];
  screenWidth?: number;
  screenHeight?: number;
}

function normalizePoint(value: number, max?: number): number {
  if (max !== undefined && value >= 0 && value <= 1) {
    return Math.round(value * max);
  }
  return Math.round(value);
}

function parseNutKey(token: string): Key | null {
  const lower = token.toLowerCase();
  switch (lower) {
    case "ctrl":
    case "control":
      return Key.LeftControl;
    case "alt":
      return Key.LeftAlt;
    case "shift":
      return Key.LeftShift;
    case "meta":
    case "win":
    case "cmd":
      return Key.LeftSuper;
    case "enter":
      return Key.Enter;
    case "tab":
      return Key.Tab;
    case "esc":
    case "escape":
      return Key.Escape;
    case "delete":
      return Key.Delete;
    case "backspace":
      return Key.Backspace;
    case "f4":
      return Key.F4;
    default:
      if (lower.length === 1) {
        // Map single character to Key enum value
        const charCode = lower.charCodeAt(0);
        if (charCode >= 97 && charCode <= 122) {
          // a-z: Key.A is typically 4, Key.B is 5, etc.
          return (Key.A + (charCode - 97)) as unknown as Key;
        }
        return null;
      }
      return null;
  }
}

export async function performMouseAction(payload: ActionPayload): Promise<Record<string, unknown>> {
  const { action, x, y, delta, screenWidth, screenHeight } = payload;

  switch (action) {
    case "click": {
      if (x !== undefined && y !== undefined) {
        const nx = normalizePoint(x, screenWidth);
        const ny = normalizePoint(y, screenHeight);
        await mouse.setPosition(new Point(nx, ny));
      }
      await mouse.click(Button.LEFT);
      break;
    }
    case "double_click": {
      if (x !== undefined && y !== undefined) {
        const nx = normalizePoint(x, screenWidth);
        const ny = normalizePoint(y, screenHeight);
        await mouse.setPosition(new Point(nx, ny));
      }
      await mouse.click(Button.LEFT);
      await sleep(80);
      await mouse.click(Button.LEFT);
      break;
    }
    case "right_click": {
      if (x !== undefined && y !== undefined) {
        const nx = normalizePoint(x, screenWidth);
        const ny = normalizePoint(y, screenHeight);
        await mouse.setPosition(new Point(nx, ny));
      }
      await mouse.click(Button.RIGHT);
      break;
    }
    case "move_mouse": {
      if (x === undefined || y === undefined) {
        throw new Error("move_mouse requires x and y");
      }
      const nx = normalizePoint(x, screenWidth);
      const ny = normalizePoint(y, screenHeight);
      await mouse.setPosition(new Point(nx, ny));
      break;
    }
    case "scroll": {
      const scrollDelta = delta ?? 0;
      await mouse.scrollUp(scrollDelta);
      break;
    }
    default:
      throw new Error(`unsupported mouse action: ${action}`);
  }

  return { ok: true, action };
}

export async function performKeyboardAction(payload: ActionPayload): Promise<Record<string, unknown>> {
  const { action, text, keys } = payload;

  switch (action) {
    case "type_text": {
      if (!text) throw new Error("type_text requires text");
      await keyboard.type(text);
      break;
    }
    case "press_keys": {
      if (!keys || keys.length === 0) throw new Error("press_keys requires keys");
      const parsed = keys
        .map(parseNutKey)
        .filter((k): k is Key => k !== null);
      if (parsed.length === 0) throw new Error("no valid keys to press");

      // Press modifier keys, then click the last key, then release modifiers
      const modifiers = parsed.slice(0, -1);
      const lastKey = parsed[parsed.length - 1];

      for (const key of modifiers) {
        await keyboard.pressKey(key);
      }
      await keyboard.pressKey(lastKey);
      await keyboard.releaseKey(lastKey);
      for (const key of modifiers.reverse()) {
        await keyboard.releaseKey(key);
      }
      break;
    }
    default:
      throw new Error(`unsupported keyboard action: ${action}`);
  }

  return { ok: true, action };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
