// SPDX-License-Identifier: GPL-3.0-only

import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function Modal({
  title,
  position = "center",
  onClose,
  children,
}: {
  title: string;
  position?: "center" | "lower-center";
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Accessibility: move focus into the dialog on open, close on Escape,
  // and restore focus to the triggering element on unmount.
  useEffect(() => {
    triggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    closeButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      triggerRef.current?.focus();
    };
  }, []);

  return createPortal(
    <div
      className={`modal-backdrop modal-backdrop-${position}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className={`map-modal map-modal-${position}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header className="modal-heading">
          <h2 id={titleId}>{title}</h2>
          <button
            ref={closeButtonRef}
            type="button"
            className="modal-close"
            aria-label={`Close ${title}`}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="modal-content">{children}</div>
      </section>
    </div>,
    document.body,
  );
}
