// SPDX-License-Identifier: GPL-3.0-only

// Modal tests: focus management, Escape/backdrop closing, accessibility.

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Modal } from "./Modal";

describe("Modal", () => {
  it.each(["button", "Escape", "backdrop"] as const)(
    "closes via %s and returns focus to its trigger",
    async (method) => {
      const user = userEvent.setup();
      render(<ModalHarness />);
      const trigger = screen.getByRole("button", { name: "Open details" });

      await user.click(trigger);
      const dialog = screen.getByRole("dialog", { name: "Details" });
      if (method === "button") {
        await user.click(screen.getByRole("button", { name: "Close Details" }));
      } else if (method === "Escape") {
        await user.keyboard("{Escape}");
      } else {
        fireEvent.mouseDown(dialog.parentElement!);
      }

      expect(screen.queryByRole("dialog", { name: "Details" })).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
    },
  );
});

function ModalHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open details</button>
      {open && (
        <Modal title="Details" onClose={() => setOpen(false)}>
          <p>Modal content</p>
        </Modal>
      )}
    </>
  );
}
