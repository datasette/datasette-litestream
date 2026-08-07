// Shapes of the datasette-litestream JSON API, aliased from the generated
// frontend/api.d.ts (regenerate with `just types-routes`). The Python models
// in datasette_litestream/_models.py are the source of truth.

import type { components, paths } from "../../api.d.ts";

export type DaemonInfo = components["schemas"]["DaemonInfo"];
export type ManagedDatabase = components["schemas"]["ManagedDatabase"];
export type AvailableDatabase = components["schemas"]["AvailableDatabase"];

// Status and ActionResult are inlined into the operations (only nested models
// land in components.schemas), so alias them off the paths they come from.
export type Status =
  paths["/-/litestream/api/status"]["get"]["responses"]["200"]["content"]["application/json"];
export type ActionResult =
  paths["/-/litestream/api/sync"]["post"]["responses"]["200"]["content"]["application/json"];

// Inline page data embedded by the litestream_admin.html template (not part
// of the JSON API, so not generated).
export interface PageData {
  can_manage: boolean;
  actor: Record<string, unknown> | null;
}

// A database an action can address: an attached database by name, or the
// internal database via the `internal` flag (it has no addressable name).
export interface Target {
  database?: string | null;
  internal?: boolean;
}

export function targetLabel(t: Target): string {
  return t.internal ? "internal database" : (t.database ?? "");
}
