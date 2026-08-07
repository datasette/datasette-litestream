<script lang="ts">
  import type { PageData, Status, ActionResult, Target } from "./lib/types";
  import { targetLabel } from "./lib/types";
  import { client } from "./lib/client";
  import DaemonCard from "./lib/DaemonCard.svelte";
  import DatabaseTable from "./lib/DatabaseTable.svelte";
  import AddDatabase from "./lib/AddDatabase.svelte";

  let { pageData }: { pageData: PageData } = $props();

  let status = $state<Status | null>(null);
  let loadError = $state<string | null>(null);
  let notice = $state<string | null>(null);
  let busy = $state<string | null>(null);

  const POLL_MS = 2000;

  // canManage comes from the server-rendered page; the status payload confirms it.
  const canManage = $derived(
    (status?.can_manage ?? pageData.can_manage) === true,
  );

  async function refresh() {
    try {
      const { data, response } = await client.GET("/-/litestream/api/status");
      if (data === undefined) {
        loadError = `HTTP ${response.status}`;
        return;
      }
      status = data;
      loadError = null;
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
  }

  $effect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  });

  // Non-2xx responses carry the API's {ok: false, error} body in `error`;
  // Pydantic validation failures carry {error, errors} with no `ok` field.
  // Returns whether the action succeeded, so callers (the register form)
  // can reset their own state.
  async function run(
    label: string,
    fn: () => Promise<{
      data?: ActionResult;
      error?: unknown;
      response: Response;
    }>,
  ): Promise<boolean> {
    busy = label;
    notice = null;
    let ok = false;
    try {
      const { data, error, response } = await fn();
      if (data?.ok) {
        notice = `${label}: ${data.status ?? "ok"}`;
        ok = true;
      } else {
        const err = (error ?? {}) as { error?: string };
        notice = `Error: ${data?.error ?? err.error ?? `HTTP ${response.status}`}`;
      }
    } catch (e) {
      notice = `Error: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      busy = null;
      await refresh();
    }
    return ok;
  }

  // The internal database is addressed with {internal: true}, never by name.
  const targetBody = (t: Target) =>
    t.internal ? { internal: true as const } : { database: t.database ?? "" };

  const onsync = (t: Target) =>
    run(targetLabel(t), () =>
      client.POST("/-/litestream/api/sync", { body: targetBody(t) }),
    );
  const onstop = (t: Target) =>
    run(targetLabel(t), () =>
      client.POST("/-/litestream/api/stop", { body: targetBody(t) }),
    );
  const onstart = (t: Target) =>
    run(targetLabel(t), () =>
      client.POST("/-/litestream/api/start", { body: targetBody(t) }),
    );
  const onunregister = (t: Target) =>
    run(targetLabel(t), () =>
      client.POST("/-/litestream/unregister", { body: targetBody(t) }),
    );
  const onregister = (t: Target, replica: string) =>
    run(targetLabel(t), () =>
      client.POST("/-/litestream/register", {
        body: replica ? { ...targetBody(t), replica } : targetBody(t),
      }),
    );
</script>

<div class="ls-app">
  <h1>Litestream</h1>

  {#if status && !status.running}
    <p class="ls-banner ls-warn">Litestream is not running.</p>
  {:else if loadError}
    <p class="ls-banner ls-warn">Could not load status: {loadError}</p>
  {:else if status}
    {#if status.socket_error}
      <p class="ls-banner ls-warn">Control socket error: {status.socket_error}</p>
    {/if}
    {#each status.warnings ?? [] as warning}
      <p class="ls-banner ls-warn">⚠ {warning}</p>
    {/each}
    {#if !canManage}
      <p class="ls-banner ls-info">
        You have read-only access. Managing databases requires the
        <code>litestream-manage</code> permission.
      </p>
    {/if}
    {#if notice}
      <p class="ls-banner ls-info" role="status">{notice}</p>
    {/if}

    <DaemonCard daemon={status.daemon} />
    <DatabaseTable
      databases={status.databases ?? []}
      {canManage}
      {busy}
      {onsync}
      {onstop}
      {onstart}
      {onunregister}
    />
    {#if canManage}
      <AddDatabase available={status.available ?? []} {busy} {onregister} />
    {/if}
  {:else}
    <p class="ls-muted">Loading…</p>
  {/if}
</div>

<style>
  .ls-app {
    max-width: 40rem;
    margin: 0 auto;
  }
  .ls-app h1 {
    margin-bottom: 1rem;
  }
  .ls-banner {
    padding: 0.6rem 0.9rem;
    border-radius: 6px;
    margin-bottom: 1rem;
    font-size: 0.9rem;
  }
  .ls-warn {
    background: #fbe9e7;
    color: #8a2a1a;
  }
  .ls-info {
    background: #e8f0fe;
    color: #1a3a7a;
  }
  .ls-muted {
    color: #777;
  }
</style>
