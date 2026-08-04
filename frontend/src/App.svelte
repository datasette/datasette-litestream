<script lang="ts">
  import type { PageData, Status, ActionResult } from "./lib/types";
  import * as api from "./lib/api";
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
      status = await api.getStatus();
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

  async function run(database: string, fn: () => Promise<ActionResult>) {
    busy = database;
    notice = null;
    try {
      const result = await fn();
      if (!result.ok) {
        notice = `Error: ${result.error ?? "request failed"}`;
      } else {
        notice = `${database}: ${result.status ?? "ok"}`;
      }
    } catch (e) {
      notice = `Error: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      busy = null;
      await refresh();
    }
  }

  const onsync = (db: string) => run(db, () => api.syncDatabase(db));
  const onstop = (db: string) => run(db, () => api.stopDatabase(db));
  const onstart = (db: string) => run(db, () => api.startDatabase(db));
  const onunregister = (db: string) => run(db, () => api.unregisterDatabase(db));
  const onregister = (db: string, replica: string) =>
    run(db, () => api.registerDatabase(db, replica));
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
    max-width: 60rem;
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
