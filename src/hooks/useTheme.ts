import { useEffect, useMemo, useState } from "react";
import type { ThemeMode } from "../types/protocol";

function getSystemPrefersDark() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function useTheme(themeMode: ThemeMode) {
  const [prefersDark, setPrefersDark] = useState(() =>
    typeof window !== "undefined" ? getSystemPrefersDark() : true,
  );

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => {
      setPrefersDark(event.matches);
    };

    setPrefersDark(media.matches);
    media.addEventListener("change", onChange);

    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);

  const resolvedTheme = useMemo(() => {
    if (themeMode === "system") {
      return prefersDark ? "dark" : "light";
    }
    return themeMode;
  }, [prefersDark, themeMode]);

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
  }, [resolvedTheme]);

  return resolvedTheme;
}
