// SPDX-License-Identifier: GPL-3.0-only

import { Modal } from "./Modal";

export function DeleteSessionDialog({
  question,
  deleting,
  onCancel,
  onConfirm,
}: {
  question: string;
  deleting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal title="Delete Question Session" onClose={onCancel}>
      <div className="delete-confirmation">
        <p>This permanently removes the session from your history:</p>
        <blockquote>{question}</blockquote>
        <div className="delete-confirmation-actions">
          <button
            type="button"
            className="secondary-action"
            disabled={deleting}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className="danger-action"
            disabled={deleting}
            onClick={onConfirm}
          >
            {deleting ? "Deleting…" : "Delete session"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
