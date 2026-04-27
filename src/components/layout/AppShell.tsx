import type { ReactNode } from "react";

interface AppShellProps {
  sidebarPanel: ReactNode;
  mainPanel: ReactNode;
  inspectorPanel: ReactNode;
  inspectorCollapsed: boolean;
}

export function AppShell(props: AppShellProps) {
  const { sidebarPanel, mainPanel, inspectorPanel, inspectorCollapsed } = props;

  return (
    <main
      className={
        inspectorCollapsed ? "app-shell inspector-collapsed" : "app-shell"
      }
    >
      <div className="app-shell-sidebar">{sidebarPanel}</div>
      <div className="app-shell-main">{mainPanel}</div>
      <div className="app-shell-inspector">{inspectorPanel}</div>
    </main>
  );
}
