<script lang="ts">
  import type { AvailableDatabase } from "./types";

  let {
    available,
    busy,
    onregister,
  }: {
    available: AvailableDatabase[];
    busy: string | null;
    onregister: (db: string, replica: string) => void;
  } = $props();

  let selected = $state("");
  let replica = $state("");

  // When the selected database changes, prefill the replica with its suggestion.
  $effect(() => {
    const match = available.find((a) => a.database === selected);
    if (match && match.suggested_replica && !replica) {
      replica = match.suggested_replica;
    }
  });

  function submit(event: Event) {
    event.preventDefault();
    if (!selected || !replica.trim()) return;
    onregister(selected, replica.trim());
  }
</script>

<div class="ls-card">
  <h2>Add a database</h2>
  {#if available.length === 0}
    <p class="ls-muted">
      Every attached database is already replicating (or none are eligible).
    </p>
  {:else}
    <form class="ls-form" onsubmit={submit}>
      <label>
        Database
        <select bind:value={selected}>
          <option value="" disabled>Choose a database…</option>
          {#each available as a (a.path)}
            <option value={a.database}>{a.database}</option>
          {/each}
        </select>
      </label>
      <label class="ls-grow">
        Replica URL
        <input
          type="text"
          bind:value={replica}
          placeholder="s3://bucket/prefix or file:///backups/db"
        />
      </label>
      <button type="submit" disabled={!selected || !replica.trim() || busy === selected}>
        Register
      </button>
    </form>
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
  .ls-form {
    display: flex;
    gap: 1rem;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .ls-form label {
    display: flex;
    flex-direction: column;
    font-size: 0.78rem;
    color: #555;
    gap: 0.25rem;
  }
  .ls-form .ls-grow {
    flex: 1;
    min-width: 16rem;
  }
  .ls-form input,
  .ls-form select {
    padding: 0.35rem 0.5rem;
    font-size: 0.9rem;
  }
  .ls-muted {
    color: #777;
  }
</style>
