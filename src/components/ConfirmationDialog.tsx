import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

interface ConfirmationDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmationDialog(props: ConfirmationDialogProps) {
  const {
    open,
    title,
    description,
    confirmLabel = "确认删除",
    onCancel,
    onConfirm,
  } = props;
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;

    confirmButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onCancel, open]);

  if (!open || typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div
      className="confirmation-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <section
        aria-describedby="confirmation-dialog-description"
        aria-labelledby="confirmation-dialog-title"
        aria-modal="true"
        className="confirmation-dialog"
        role="alertdialog"
      >
        <span className="card-kicker">需要确认</span>
        <strong id="confirmation-dialog-title">{title}</strong>
        <p id="confirmation-dialog-description">{description}</p>
        <div className="confirmation-dialog-actions">
          <button className="ghost-button" onClick={onCancel} type="button">
            取消
          </button>
          <button
            className="primary-button danger"
            onClick={onConfirm}
            ref={confirmButtonRef}
            type="button"
          >
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>,
    document.body,
  );
}
