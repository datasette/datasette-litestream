import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import DatabaseTable from "../DatabaseTable.svelte";
import type { ManagedDatabase } from "../types";

// jsdom does not implement <dialog>'s modal API.
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});

function row(overrides: Partial<ManagedDatabase> = {}): ManagedDatabase {
  return {
    database: "data",
    internal: false,
    path: "/tmp/data.db",
    status: "replicating",
    last_sync_at: "2026-06-09T00:00:00Z",
    replica: "file:///tmp/backups/data",
    ...overrides,
  };
}

function props(databases: ManagedDatabase[]) {
  return {
    databases,
    canManage: true,
    busy: null,
    onsync: vi.fn(),
    onstop: vi.fn(),
    onstart: vi.fn(),
    onunregister: vi.fn(),
  };
}

describe("DatabaseTable details dialog", () => {
  it("reflects the latest poll data while open", async () => {
    const { rerender } = render(DatabaseTable, { props: props([row()]) });
    await fireEvent.click(screen.getByTitle("Show details"));
    const dialog = document.querySelector("dialog")!;
    expect(within(dialog).getByText("replicating")).toBeTruthy();

    // A poll updates the row: the open dialog must not stay frozen.
    await rerender(props([row({ status: "stopped" })]));
    await waitFor(() =>
      expect(within(dialog).getByText("stopped")).toBeTruthy(),
    );
  });

  it("closes when its row disappears from the table", async () => {
    const { rerender } = render(DatabaseTable, { props: props([row()]) });
    await fireEvent.click(screen.getByTitle("Show details"));
    const dialog = document.querySelector("dialog") as HTMLDialogElement;
    expect(dialog.open).toBe(true);

    await rerender(props([]));
    await waitFor(() => expect(dialog.open).toBe(false));
  });
});
