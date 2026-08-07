import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import AddDatabase from "../AddDatabase.svelte";
import type { AvailableDatabase } from "../types";

const AVAILABLE: AvailableDatabase[] = [
  { database: "a", path: "/tmp/a.db", suggested_replica: "file:///backups/a" },
  { database: "b", path: "/tmp/b.db", suggested_replica: "file:///backups/b" },
];

function setup(onregister = vi.fn(async () => true)) {
  render(AddDatabase, {
    props: { available: AVAILABLE, busy: null, onregister },
  });
  const select = screen.getByRole("combobox") as HTMLSelectElement;
  const input = screen.getByRole("textbox") as HTMLInputElement;
  const button = screen.getByRole("button", { name: "Register" });
  return { select, input, button, onregister };
}

describe("AddDatabase", () => {
  it("prefills the suggestion and follows the selection", async () => {
    const { select, input } = setup();
    await fireEvent.change(select, { target: { value: "/tmp/a.db" } });
    await waitFor(() => expect(input.value).toBe("file:///backups/a"));
    // Switching must replace the previous database's suggestion, not keep it.
    await fireEvent.change(select, { target: { value: "/tmp/b.db" } });
    await waitFor(() => expect(input.value).toBe("file:///backups/b"));
  });

  it("preserves a hand-edited replica across selection changes", async () => {
    const { select, input } = setup();
    await fireEvent.change(select, { target: { value: "/tmp/a.db" } });
    await waitFor(() => expect(input.value).toBe("file:///backups/a"));
    await fireEvent.input(input, { target: { value: "s3://custom/x" } });
    await fireEvent.change(select, { target: { value: "/tmp/b.db" } });
    // User input wins over the new suggestion.
    expect(input.value).toBe("s3://custom/x");
  });

  it("submits the selected target and clears the form on success", async () => {
    const { select, input, button, onregister } = setup();
    await fireEvent.change(select, { target: { value: "/tmp/b.db" } });
    await waitFor(() => expect(input.value).toBe("file:///backups/b"));
    await fireEvent.click(button);
    await waitFor(() => expect(onregister).toHaveBeenCalledWith(
      AVAILABLE[1],
      "file:///backups/b",
    ));
    await waitFor(() => {
      expect(select.value).toBe("");
      expect(input.value).toBe("");
    });
  });

  it("keeps the form state when registration fails", async () => {
    const { select, input, button } = setup(vi.fn(async () => false));
    await fireEvent.change(select, { target: { value: "/tmp/a.db" } });
    await waitFor(() => expect(input.value).toBe("file:///backups/a"));
    await fireEvent.click(button);
    // Failure: the user gets to retry with what they typed.
    await waitFor(() => expect(input.value).toBe("file:///backups/a"));
    expect(select.value).toBe("/tmp/a.db");
  });
});
