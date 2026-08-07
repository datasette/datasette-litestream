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

const INTERNAL_ROW = {
  database: null,
  internal: true,
  path: "/tmp/internal.db",
  status: "replicating",
  last_sync_at: "2026-06-09T00:00:00Z",
  replica: "file:///tmp/backups/internal",
};

const DETACHED_ROW = {
  database: null,
  internal: false,
  path: "/tmp/gone.db",
  status: "replicating",
  last_sync_at: null,
  replica: null,
};

/** Like mockFetch, but records every POST body for request-shape assertions. */
function mockFetchCapture(
  handler: (url: string) => unknown,
  bodies: Array<{ url: string; body: Record<string, unknown> | null }>,
) {
  return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input);
    const method =
      input instanceof Request ? input.method : (init?.method ?? "GET");
    if (method === "POST") {
      let body: Record<string, unknown> | null = null;
      if (input instanceof Request) {
        body = await input
          .clone()
          .json()
          .catch(() => null);
      } else if (typeof init?.body === "string") {
        body = JSON.parse(init.body);
      }
      bodies.push({ url, body });
    }
    return new Response(JSON.stringify(handler(url)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
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

  it("addresses the internal database with {internal: true}, never by name", async () => {
    const bodies: Array<{ url: string; body: Record<string, unknown> | null }> =
      [];
    const fetchMock = mockFetchCapture((url) => {
      if (url.includes("/api/sync"))
        return { ok: true, internal: true, status: "synced" };
      return statusPayload({ databases: [INTERNAL_ROW] });
    }, bodies);
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("internal database")).toBeTruthy());

    await fireEvent.click(screen.getByText("Sync"));
    await waitFor(() => {
      const sync = bodies.find((b) => b.url.includes("/api/sync"));
      expect(sync?.body).toEqual({ internal: true });
    });
  });

  it("registers the internal database from the form with {internal: true}", async () => {
    const bodies: Array<{ url: string; body: Record<string, unknown> | null }> =
      [];
    const fetchMock = mockFetchCapture((url) => {
      if (url.includes("/register"))
        return { ok: true, internal: true, status: "registered" };
      return statusPayload({
        available: [
          {
            database: null,
            internal: true,
            path: "/tmp/internal.db",
            suggested_replica: null,
          },
        ],
      });
    }, bodies);
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByRole("combobox")).toBeTruthy());

    await fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "/tmp/internal.db" },
    });
    await fireEvent.input(screen.getByRole("textbox"), {
      target: { value: "file:///tmp/backups/internal" },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() => {
      const register = bodies.find((b) => b.url.includes("/register"));
      expect(register?.body).toEqual({
        internal: true,
        replica: "file:///tmp/backups/internal",
      });
    });
  });

  it("named databases post {database: name} without the internal flag", async () => {
    const bodies: Array<{ url: string; body: Record<string, unknown> | null }> =
      [];
    const fetchMock = mockFetchCapture((url) => {
      if (url.includes("/unregister"))
        return { ok: true, database: "data", status: "unregistered" };
      return statusPayload();
    }, bodies);
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("Remove")).toBeTruthy());

    await fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => {
      const unregister = bodies.find((b) => b.url.includes("/unregister"));
      expect(unregister?.body).toEqual({ database: "data" });
    });
  });

  it("renders detached databases without action buttons", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(() => statusPayload({ databases: [DETACHED_ROW] })),
    );
    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("detached")).toBeTruthy());
    expect(screen.queryByText("Sync")).toBeNull();
    expect(screen.queryByText("Remove")).toBeNull();
  });

  it("shows the API's error body in the notice on a failed action", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/sync")) {
        return new Response(
          JSON.stringify({ ok: false, error: "daemon exploded" }),
          { status: 502, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify(statusPayload()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("Sync")).toBeTruthy());
    await fireEvent.click(screen.getByText("Sync"));
    await waitFor(() =>
      expect(screen.getByText(/error: daemon exploded/i)).toBeTruthy(),
    );
  });

  it("falls back to the HTTP status for non-JSON action failures", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/sync")) {
        return new Response("<html>gateway error</html>", { status: 500 });
      }
      return new Response(JSON.stringify(statusPayload()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("Sync")).toBeTruthy());
    await fireEvent.click(screen.getByText("Sync"));
    await waitFor(() => expect(screen.getByText(/error: http 500/i)).toBeTruthy());
  });

  it("surfaces a thrown fetch as an error notice", async () => {
    let failActions = false;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (failActions && url.includes("/api/sync")) {
        throw new TypeError("network down");
      }
      return new Response(JSON.stringify(statusPayload()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(App, { props: { pageData: { can_manage: true, actor: { id: "root" } } } });
    await waitFor(() => expect(screen.getByText("Sync")).toBeTruthy());
    failActions = true;
    await fireEvent.click(screen.getByText("Sync"));
    await waitFor(() =>
      expect(screen.getByText(/error: network down/i)).toBeTruthy(),
    );
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
