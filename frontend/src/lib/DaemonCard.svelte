<script lang="ts">
  import type { DaemonInfo } from "./types";
  import { formatUptime } from "./format";

  let { daemon }: { daemon: DaemonInfo | null | undefined } = $props();
</script>

<div class="ls-card">
  <h2>Daemon</h2>
  {#if daemon}
    <dl class="ls-meta">
      <div><dt>Version</dt><dd>{daemon.version}</dd></div>
      <div><dt>PID</dt><dd>{daemon.pid}</dd></div>
      <div><dt>Uptime</dt><dd>{formatUptime(daemon.uptime_seconds)}</dd></div>
      <div><dt>Databases</dt><dd>{daemon.database_count}</dd></div>
    </dl>
  {:else}
    <p class="ls-muted">Daemon details unavailable.</p>
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
  .ls-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    margin: 0;
  }
  .ls-meta dt {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #777;
  }
  .ls-meta dd {
    margin: 0;
    font-size: 1.1rem;
    font-variant-numeric: tabular-nums;
  }
  .ls-muted {
    color: #777;
  }
</style>
