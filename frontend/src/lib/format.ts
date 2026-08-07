export function formatUptime(seconds: number | undefined): string {
  if (seconds == null) return "—";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

// Truncate in the middle ("/Users/alex/…/demo.db") so both the start and the
// distinguishing tail of long paths/URLs stay visible.
export function middleTruncate(text: string, max: number): string {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  const tail = keep - head;
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`;
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  const secs = Math.max(0, Math.round((now - d.getTime()) / 1000));
  const units: [number, string][] = [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
    [1, "second"],
  ];
  for (const [size, label] of units) {
    if (secs >= size || size === 1) {
      const n = Math.floor(secs / size);
      return `${n} ${label}${n === 1 ? "" : "s"} ago`;
    }
  }
  return null;
}

// POSIX single-quoting: close the quote, emit an escaped quote, reopen.
// Replica URLs and filenames land in a copy-pasteable shell command; a
// manage-permission user can register a URL containing quotes/metacharacters,
// so unescaped interpolation would hand admins a command that executes it.
function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

export function restoreCommand(path: string, replica: string): string {
  const filename = path.split("/").pop() || "restored.db";
  return `litestream restore -o ${shellQuote(filename)} ${shellQuote(replica)}`;
}
