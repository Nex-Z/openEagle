import { useCallback, useRef, type ReactNode } from "react";

interface AppShellProps {
  sidebarPanel: ReactNode;
  mainPanel: ReactNode;
  inspectorPanel: ReactNode;
  inspectorCollapsed: boolean;
  sidebarWidth: number;
  inspectorWidth: number;
  onSidebarWidthChange: (width: number) => void;
  onInspectorWidthChange: (width: number) => void;
}

const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 400;
const INSPECTOR_MIN = 240;
const INSPECTOR_MAX = 500;
const SIDEBAR_DEFAULT = 248;
const INSPECTOR_DEFAULT = 318;
// 折叠态宽度：放一个胶囊展开按钮的窄卡片列
const INSPECTOR_COLLAPSED_WIDTH = 52;
// 栏间间隙宽度（同时作为拖拽热区列）
const GAP_WIDTH = 12;

function ResizeHandle(props: {
  onResize: (delta: number) => void;
  onResizeEnd: () => void;
  onDoubleClick: () => void;
  direction: "left" | "right";
}) {
  const { onResize, onResizeEnd, onDoubleClick, direction } = props;
  const draggingRef = useRef(false);
  const handleRef = useRef<HTMLDivElement>(null);

  const handleMouseDown = useCallback(
    (event: React.MouseEvent) => {
      event.preventDefault();
      draggingRef.current = true;
      handleRef.current?.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const startX = event.clientX;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        if (!draggingRef.current) return;
        const delta = moveEvent.clientX - startX;
        const signedDelta = direction === "right" ? delta : -delta;
        onResize(signedDelta);
      };

      const handleMouseUp = () => {
        draggingRef.current = false;
        handleRef.current?.classList.remove("is-dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        onResizeEnd();
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
      };

      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    },
    [direction, onResize, onResizeEnd],
  );

  return (
    <div
      ref={handleRef}
      className="resize-handle"
      onMouseDown={handleMouseDown}
      onDoubleClick={onDoubleClick}
    />
  );
}

export function AppShell(props: AppShellProps) {
  const {
    sidebarPanel,
    mainPanel,
    inspectorPanel,
    inspectorCollapsed,
    sidebarWidth,
    inspectorWidth,
    onSidebarWidthChange,
    onInspectorWidthChange,
  } = props;

  const startSidebarRef = useRef(sidebarWidth);
  const startInspectorRef = useRef(inspectorWidth);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      const next = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, startSidebarRef.current + delta));
      onSidebarWidthChange(next);
    },
    [onSidebarWidthChange],
  );

  const handleInspectorResize = useCallback(
    (delta: number) => {
      const next = Math.max(INSPECTOR_MIN, Math.min(INSPECTOR_MAX, startInspectorRef.current + delta));
      onInspectorWidthChange(next);
    },
    [onInspectorWidthChange],
  );

  const handleSidebarResizeEnd = useCallback(() => {
    startSidebarRef.current = sidebarWidth;
  }, [sidebarWidth]);

  const handleInspectorResizeEnd = useCallback(() => {
    startInspectorRef.current = inspectorWidth;
  }, [inspectorWidth]);

  const effectiveInspectorWidth = inspectorCollapsed ? INSPECTOR_COLLAPSED_WIDTH : inspectorWidth;

  return (
    <main
      className={
        inspectorCollapsed
          ? "app-shell inspector-collapsed text-slate-950"
          : "app-shell text-slate-950"
      }
      style={{
        gridTemplateColumns: `${sidebarWidth}px ${GAP_WIDTH}px minmax(0, 1fr) ${GAP_WIDTH}px ${effectiveInspectorWidth}px`,
      }}
    >
      <div className="app-shell-sidebar">{sidebarPanel}</div>
      <ResizeHandle
        direction="right"
        onResize={handleSidebarResize}
        onResizeEnd={handleSidebarResizeEnd}
        onDoubleClick={() => onSidebarWidthChange(SIDEBAR_DEFAULT)}
      />
      <div className="app-shell-main">{mainPanel}</div>
      <ResizeHandle
        direction="left"
        onResize={handleInspectorResize}
        onResizeEnd={handleInspectorResizeEnd}
        onDoubleClick={() => onInspectorWidthChange(INSPECTOR_DEFAULT)}
      />
      <div className="app-shell-inspector">{inspectorPanel}</div>
    </main>
  );
}
