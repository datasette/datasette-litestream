// Shapes returned by the datasette-litestream backend JSON API. These mirror
// the payloads built in datasette_litestream/__init__.py (_build_status etc.).

export interface DaemonInfo {
  version: string;
  pid: number;
  uptime_seconds: number;
  started_at: string;
  database_count: number;
}

export interface ManagedDatabase {
  database: string | null; // Datasette database name, if attached
  path: string;
  status: string | null; // e.g. "replicating"
  last_sync_at: string | null;
  replica: string | null;
}

export interface AvailableDatabase {
  database: string;
  path: string;
  suggested_replica: string | null;
}

export interface Status {
  running: boolean;
  can_manage?: boolean;
  metrics_enabled?: boolean;
  daemon?: DaemonInfo | null;
  socket_error?: string | null;
  databases?: ManagedDatabase[];
  available?: AvailableDatabase[];
  warnings?: string[];
}

export interface ActionResult {
  ok: boolean;
  error?: string;
  details?: string | null;
  database?: string;
  status?: string;
  result?: unknown;
}

export interface PageData {
  can_manage: boolean;
  actor: Record<string, unknown> | null;
}
