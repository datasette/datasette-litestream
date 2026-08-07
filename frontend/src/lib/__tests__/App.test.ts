import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/svelte";
import App from "../../App.svelte";
import type { Status } from "../types";

function statusPayload(overrides: Partial<Status> = {}): Status {
  return {
    running: true,
    can_manage: true,
    metrics_enabled: false,
    daemon: {
      version: "v0.5.12",
      pid: 1234,
      uptime_seconds: 3700,
      started_at: "2026-06-09T00:00:00Z",
      database_count: 1,
    },
    socket_error: null,
    databases: [
      {
        database: "data",
        path: "/tmp/data.db",
        status: "replicating",
        last_sync_at: "2026-06-09T00:00:00Z",
        replica: "file:///tmp/backups/data",
      },
    ],
    available: [
      { database: "extra", path: "/tmp/extra.db", suggested_replica: "file:///tmp/backups/extra" },
    ],
    ...overrides,
  };
}

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  // The generated API client calls fetch with a Request object and reads the
  // Content-Type header when parsing, so return a real Response.
  return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input);
    const body = handler(url, init);
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

describe("App", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("renders the daemon and managed databases", async () => {
    vi.stubGlobal("fetch", mockFetch(() => statusPayload()));

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });

    await waitFor(() => {
      expect(screen.getByText("v0.5.12")).toBeTruthy();
      expect(screen.getByText("Replicating databases")).toBeTruthy();
      expect(screen.getByText("data")).toBeTruthy();
    });
    // manage controls present
    expect(screen.getByText("Add a database")).toBeTruthy();
  });

  it("hides management controls in read-only mode", async () => {
    vi.stubGlobal("fetch", mockFetch(() => statusPayload({ can_manage: false })));

    render(App, { props: { pageData: { can_manage: false, actor: null } } });

    await waitFor(() => {
      expect(screen.getByText("Replicating databases")).toBeTruthy();
    });
    expect(screen.queryByText("Add a database")).toBeNull();
    expect(screen.getByText(/read-only access/i)).toBeTruthy();
  });

  it("posts a sync action when the Sync button is clicked", async () => {
    const calls: string[] = [];
    const fetchMock = mockFetch((url) => {
      calls.push(url);
      if (url.includes("/api/sync")) return { ok: true, database: "data", status: "synced" };
      return statusPayload();
    });
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("Sync")).toBeTruthy());

    await fireEvent.click(screen.getByText("Sync"));
    await waitFor(() => expect(calls.some((u) => u.includes("/api/sync"))).toBe(true));
  });

  it("shows a banner when litestream is not running", async () => {
    vi.stubGlobal("fetch", mockFetch(() => ({ running: false })));
    render(App, { props: { pageData: { can_manage: true, actor: null } } });
    await waitFor(() => expect(screen.getByText(/not running/i)).toBeTruthy());
  });

  it("keeps the dashboard visible when a later poll fails", async () => {
    let polls = 0;
    const fetchMock = vi.fn(async () => {
      polls += 1;
      if (polls === 1) {
        return new Response(JSON.stringify(statusPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("boom", { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("data")).toBeTruthy());

    // Next poll fails: the stale dashboard must survive alongside the banner.
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() =>
      expect(screen.getByText(/could not load status/i)).toBeTruthy(),
    );
    expect(screen.getByText("data")).toBeTruthy();
    expect(screen.getByText("Replicating databases")).toBeTruthy();
  });

  it("shows a full-page error when the first load fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 403 })),
    );
    render(App, { props: { pageData: { can_manage: false, actor: null } } });
    await waitFor(() =>
      expect(screen.getByText(/could not load status/i)).toBeTruthy(),
    );
    // Nothing ever loaded: no dashboard and no perpetual "Loading…".
    expect(screen.queryByText("Replicating databases")).toBeNull();
    expect(screen.queryByText(/loading/i)).toBeNull();
  });
});
