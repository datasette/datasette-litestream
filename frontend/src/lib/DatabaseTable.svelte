<script lang="ts">
  import type { ManagedDatabase } from "./types";
  import { formatTimestamp, middleTruncate, relativeTime } from "./format";

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
    onsync: (db: string) => void;
    onstop: (db: string) => void;
    onstart: (db: string) => void;
    onunregister: (db: string) => void;
  } = $props();

  function isStopped(status: string | null): boolean {
    return (status ?? "").toLowerCase().includes("stop");
  }

  function restoreCommand(db: ManagedDatabase): string | null {
    if (!db.replica) return null;
    const filename = db.path.split("/").pop() || "restored.db";
    return `litestream restore -o ${filename} '${db.replica}'`;
  }

  // Ticks once a second so the "N seconds ago" labels stay current.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  let detail = $state<ManagedDatabase | null>(null);
  let dialogEl = $state<HTMLDialogElement | null>(null);
  let copied = $state(false);
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;

  function openDetails(db: ManagedDatabase) {
    detail = db;
    copied = false;
    dialogEl?.showModal();
  }

  async function copyCommand(command: string) {
    await navigator.clipboard.writeText(command);
    copied = true;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => (copied = false), 1500);
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
          {@const name = db.database ?? db.path}
          {@const disabled = busy === name}
          <tr>
            <td>
              <button
                class="ls-dblink"
                title="Show details"
                onclick={() => openDetails(db)}
                >{db.database ?? middleTruncate(db.path, 28)}</button
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
                {#if db.database}
                  {#if isStopped(db.status)}
                    <button {disabled} onclick={() => onstart(name)}>Start</button>
                  {:else}
                    <button {disabled} onclick={() => onsync(name)}>Sync</button>
                    <button {disabled} onclick={() => onstop(name)}>Stop</button>
                  {/if}
                  <button
                    class="ls-danger"
                    {disabled}
                    onclick={() => onunregister(name)}>Remove</button
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

<dialog class="ls-dialog" bind:this={dialogEl} onclose={() => (detail = null)}>
  {#if detail}
    {@const restore = restoreCommand(detail)}
    <h3>
      {detail.database ?? "(detached database)"}
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
            title={copied ? "Copied!" : "Copy to clipboard"}
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
