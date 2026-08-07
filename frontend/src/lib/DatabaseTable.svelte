<script lang="ts">
  import type { ManagedDatabase, Target } from "./types";
  import { targetLabel } from "./types";
  import {
    formatTimestamp,
    middleTruncate,
    relativeTime,
    restoreCommand,
  } from "./format";

  let {
    databases,
    canManage,
    busy,
    onsync,
    onstop,
    onstart,
    onunregister,
  }: {
    databases: ManagedDatabase[];
    canManage: boolean;
    busy: string | null;
    onsync: (target: Target) => void;
    onstop: (target: Target) => void;
    onstart: (target: Target) => void;
    onunregister: (target: Target) => void;
  } = $props();

  function displayName(db: ManagedDatabase): string {
    if (db.internal) return "internal database";
    return db.database ?? middleTruncate(db.path, 28);
  }

  // A row is actionable when it maps to an attached database or the internal
  // database; a detached database can only be shown.
  function isActionable(db: ManagedDatabase): boolean {
    return Boolean(db.database || db.internal);
  }

  function isStopped(status: string | null | undefined): boolean {
    return (status ?? "").toLowerCase().includes("stop");
  }

  function restoreFor(db: ManagedDatabase): string | null {
    return db.replica ? restoreCommand(db.path, db.replica) : null;
  }

  // Ticks once a second so the "N seconds ago" labels stay current.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  // The dialog tracks its row by path and derives the row data from the
  // live `databases` array, so an open dialog reflects each poll instead of
  // freezing the snapshot captured at open time.
  let detailPath = $state<string | null>(null);
  let dialogEl = $state<HTMLDialogElement | null>(null);
  let copied = $state(false);
  let copyFailed = $state(false);
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;

  const detail = $derived(
    detailPath === null
      ? null
      : (databases.find((d) => d.path === detailPath) ?? null),
  );

  // Row unregistered (elsewhere) while its dialog is open: close it rather
  // than showing stale data with no backing row.
  $effect(() => {
    if (detailPath !== null && detail === null) {
      detailPath = null;
      dialogEl?.close();
    }
  });

  function openDetails(db: ManagedDatabase) {
    detailPath = db.path;
    copied = false;
    copyFailed = false;
    dialogEl?.showModal();
  }

  async function copyCommand(command: string) {
    copyFailed = false;
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(command);
      } else {
        // Non-secure contexts (plain HTTP off localhost) have no
        // navigator.clipboard; fall back to the legacy selection path.
        const textarea = document.createElement("textarea");
        textarea.value = command;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand("copy");
        textarea.remove();
        if (!ok) throw new Error("execCommand copy failed");
      }
      copied = true;
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => (copied = false), 1500);
    } catch {
      // Communicate instead of silently doing nothing (or throwing an
      // unhandled rejection); the command stays visible to select manually.
      copyFailed = true;
    }
  }
</script>

<div class="ls-card">
  <h2>Replicating databases</h2>
  {#if databases.length === 0}
    <p class="ls-muted">No databases are currently registered for replication.</p>
  {:else}
    <table class="ls-table">
      <thead>
        <tr>
          <th>Database</th>
          <th>Status</th>
          <th>Last sync</th>
          {#if canManage}<th>Actions</th>{/if}
        </tr>
      </thead>
      <tbody>
        {#each databases as db (db.path)}
          {@const disabled = busy === targetLabel(db)}
          <tr>
            <td>
              <button
                class="ls-dblink"
                title="Show details"
                onclick={() => openDetails(db)}>{displayName(db)}</button
              >
            </td>
            <td>
              <span class="ls-status" class:ls-stopped={isStopped(db.status)}>
                {db.status ?? "—"}
              </span>
            </td>
            <td class="ls-sync">
              {formatTimestamp(db.last_sync_at)}
              {#if relativeTime(db.last_sync_at, now)}
                <div class="ls-muted">{relativeTime(db.last_sync_at, now)}</div>
              {/if}
            </td>
            {#if canManage}
              <td class="ls-actions">
                {#if isActionable(db)}
                  {#if isStopped(db.status)}
                    <button {disabled} onclick={() => onstart(db)}>Start</button>
                  {:else}
                    <button {disabled} onclick={() => onsync(db)}>Sync</button>
                    <button {disabled} onclick={() => onstop(db)}>Stop</button>
                  {/if}
                  <button
                    class="ls-danger"
                    {disabled}
                    onclick={() => onunregister(db)}>Remove</button
                  >
                {:else}
                  <span class="ls-muted">detached</span>
                {/if}
              </td>
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<dialog class="ls-dialog" bind:this={dialogEl} onclose={() => (detailPath = null)}>
  {#if detail}
    {@const restore = restoreFor(detail)}
    <h3>
      {detail.internal
        ? "internal database"
        : (detail.database ?? "(detached database)")}
      <span class="ls-status" class:ls-stopped={isStopped(detail.status)}>
        {detail.status ?? "unknown"}
      </span>
    </h3>
    <dl>
      <dt>Path</dt>
      <dd><code>{detail.path}</code></dd>
      <dt>Replica</dt>
      <dd>
        {#if detail.replica}<code>{detail.replica}</code>{:else}—{/if}
      </dd>
      <dt>Last sync</dt>
      <dd>
        {formatTimestamp(detail.last_sync_at)}
        {#if relativeTime(detail.last_sync_at, now)}
          <span class="ls-muted">· {relativeTime(detail.last_sync_at, now)}</span>
        {/if}
      </dd>
      {#if restore}
        <dt>Restore with</dt>
        <dd class="ls-restore-row">
          <code>{restore}</code>
          <button
            class="ls-copy"
            class:ls-copied={copied}
            aria-label="Copy restore command"
            title={copyFailed
              ? "Copy failed — select the command manually"
              : copied
                ? "Copied!"
                : "Copy to clipboard"}
            onclick={() => copyCommand(restore)}
          >
            {#if copied}
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="16"
                height="16"
                fill="currentColor"
                viewBox="0 0 16 16"
              >
                <path
                  fill-rule="evenodd"
                  d="M10.854 7.146a.5.5 0 0 1 0 .708l-3 3a.5.5 0 0 1-.708 0l-1.5-1.5a.5.5 0 1 1 .708-.708L7.5 9.793l2.646-2.647a.5.5 0 0 1 .708 0"
                />
                <path
                  d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1z"
                />
                <path
                  d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0z"
                />
              </svg>
            {:else}
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="16"
                height="16"
                fill="currentColor"
                viewBox="0 0 16 16"
              >
                <path
                  d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1z"
                />
                <path
                  d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0z"
                />
              </svg>
            {/if}
          </button>
          {#if copyFailed}
            <span class="ls-muted" role="alert">
              copy failed — select the command manually
            </span>
          {/if}
        </dd>
      {/if}
    </dl>
    <form method="dialog">
      <button>Close</button>
    </form>
  {/if}
</dialog>

<style>
  .ls-card {
    border: 1px solid var(--color-border, #ddd);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.25rem;
    background: var(--color-card-background, #fafafa);
  }
  .ls-card h2 {
    margin: 0 0 0.75rem;
    font-size: 1.1rem;
  }
  .ls-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  .ls-table th,
  .ls-table td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid var(--color-border, #eee);
    vertical-align: top;
  }
  .ls-dblink {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    font-weight: 600;
    color: var(--color-link, #1a4a8a);
    cursor: pointer;
    text-decoration: underline dotted;
  }
  .ls-status {
    display: inline-block;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    background: #e3f4e3;
    color: #1a6b1a;
    font-size: 0.78rem;
  }
  .ls-status.ls-stopped {
    background: #f4e6e3;
    color: #8a3a1a;
  }
  .ls-sync {
    font-size: 0.85rem;
    color: #555;
    white-space: nowrap;
  }
  .ls-actions {
    white-space: nowrap;
  }
  .ls-actions button {
    margin-right: 0.3rem;
    cursor: pointer;
  }
  .ls-danger {
    color: #a11;
  }
  .ls-muted {
    color: #777;
  }
  .ls-dialog {
    max-width: 36rem;
    border: 1px solid var(--color-border, #ccc);
    border-radius: 8px;
    padding: 1.25rem 1.5rem;
  }
  .ls-dialog::backdrop {
    background: rgba(0, 0, 0, 0.35);
  }
  .ls-dialog h3 {
    margin: 0 0 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .ls-dialog h3 .ls-status {
    font-weight: normal;
  }
  .ls-dialog dl {
    margin: 0 0 1rem;
    font-size: 0.9rem;
  }
  .ls-dialog dt {
    font-weight: 600;
    margin-top: 0.6rem;
  }
  .ls-dialog dd {
    margin: 0.15rem 0 0;
  }
  .ls-dialog code {
    display: block;
    font-size: 0.8rem;
    line-height: 1.4;
    background: var(--color-code-background, #f2f2f2);
    border: 1px solid var(--color-border, #e3e3e3);
    padding: 0.45rem 0.6rem;
    border-radius: 6px;
    word-break: break-all;
  }
  .ls-restore-row {
    display: flex;
    align-items: flex-start;
    gap: 0.4rem;
  }
  .ls-restore-row code {
    flex: 1;
  }
  .ls-copy {
    background: none;
    border: none;
    padding: 0.4rem 0.2rem;
    cursor: pointer;
    color: #555;
    flex-shrink: 0;
  }
  .ls-copy:hover {
    color: #000;
  }
  .ls-copy.ls-copied {
    color: #1a6b1a;
  }
</style>
