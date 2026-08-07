<script lang="ts">
  import type { AvailableDatabase, Target } from "./types";
  import { targetLabel } from "./types";

  let {
    available,
    busy,
    onregister,
  }: {
    available: AvailableDatabase[];
    busy: string | null;
    onregister: (target: Target, replica: string) => Promise<boolean>;
  } = $props();

  // Options are keyed by path: it is unique, and the internal database has
  // no addressable name (only the `internal` flag).
  let selectedPath = $state("");
  let replica = $state("");
  // The suggestion the input was last prefilled with — so switching the
  // selection can tell "still the previous default" (replace it) apart
  // from "hand-edited by the user" (leave it alone).
  let lastSuggestion = $state("");

  const selected = $derived(
    available.find((a) => a.path === selectedPath) ?? null,
  );

  function optionLabel(a: AvailableDatabase): string {
    return a.internal ? "internal database" : (a.database ?? a.path);
  }

  // Prefill the replica with the selection's suggestion. An empty input or
  // one still holding the previous suggestion follows the selection; a
  // hand-edited value is never overwritten. Without the lastSuggestion
  // check, switching databases would silently keep the previous database's
  // replica URL and register the new one into the old one's backup.
  $effect(() => {
    const suggestion = selected?.suggested_replica ?? "";
    if (!replica || replica === lastSuggestion) {
      replica = suggestion;
    }
    lastSuggestion = suggestion;
  });

  async function submit(event: Event) {
    event.preventDefault();
    if (!selected || !replica.trim()) return;
    const ok = await onregister(selected, replica.trim());
    if (ok) {
      // The registered database leaves `available`; clear the form so the
      // stale URL cannot leak into the next registration.
      selectedPath = "";
      replica = "";
      lastSuggestion = "";
    }
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
        <select bind:value={selectedPath}>
          <option value="" disabled>Choose a database…</option>
          {#each available as a (a.path)}
            <option value={a.path}>{optionLabel(a)}</option>
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
      <button
        type="submit"
        disabled={!selected ||
          !replica.trim() ||
          busy === (selected && targetLabel(selected))}
      >
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
