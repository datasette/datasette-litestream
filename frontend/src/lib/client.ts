// Shared openapi-fetch client for the datasette-litestream JSON API.
// Paths and request/response shapes come from the generated api.d.ts
// (`just types-routes`), so call sites are fully typed:
//
//   const { data, error } = await client.POST("/-/litestream/api/sync", {
//     body: { database },
//   });
import createClient from "openapi-fetch";
import type { paths } from "../../api.d.ts";

// The admin page is served at "<base>/-/litestream" and the API paths are
// site-absolute ("/-/litestream/api/..."), so the client's baseUrl must carry
// Datasette's base_url prefix. Derive it from the current path so the client
// works regardless of the base_url setting.
export const client = createClient<paths>({
  baseUrl:
    window.location.origin +
    window.location.pathname.replace(/\/-\/litestream\/?$/, ""),
  credentials: "same-origin",
  // Resolve fetch at request time, not client-creation time, so test stubs
  // (vi.stubGlobal) installed after this module loads still apply.
  fetch: (request) => globalThis.fetch(request),
});
