import type { ThemeMode } from "../types/protocol";

interface ThemeToggleProps {
  value: ThemeMode;
  onChange: (value: ThemeMode) => void;
}

export function ThemeToggle({ value, onChange }: ThemeToggleProps) {
  return (
    <div className="theme-toggle" role="tablist" aria-label="主题模式">
      <button
        className={value === "light" ? "theme-chip active" : "theme-chip"}
        onClick={() => onChange("light")}
        type="button"
      >
        日间
      </button>
      <button
        className={value === "dark" ? "theme-chip active" : "theme-chip"}
        onClick={() => onChange("dark")}
        type="button"
      >
        夜间
      </button>
      <button
        className={value === "system" ? "theme-chip active" : "theme-chip"}
        onClick={() => onChange("system")}
        type="button"
      >
        自动
      </button>
    </div>
  );
}
