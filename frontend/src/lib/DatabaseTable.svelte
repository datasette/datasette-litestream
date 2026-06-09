<script lang="ts">
  import type { ManagedDatabase } from "./types";
  import { formatTimestamp } from "./format";

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
          <th>Replica</th>
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
              <div class="ls-dbname">{db.database ?? "—"}</div>
              <div class="ls-path" title={db.path}>{db.path}</div>
            </td>
            <td class="ls-replica" title={db.replica ?? ""}>{db.replica ?? "—"}</td>
            <td>
              <span class="ls-status" class:ls-stopped={isStopped(db.status)}>
                {db.status ?? "—"}
              </span>
            </td>
            <td>{formatTimestamp(db.last_sync_at)}</td>
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
  .ls-dbname {
    font-weight: 600;
  }
  .ls-path,
  .ls-replica {
    font-size: 0.78rem;
    color: #777;
    font-family: var(--font-monospace, monospace);
    max-width: 22rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
</style>
