import type { PageData } from "./types";

// The backend injects <script type="application/json" id="litestream-page-data">.
export function loadPageData(): PageData {
  const el = document.getElementById("litestream-page-data");
  if (!el || !el.textContent) {
    return { can_manage: false, actor: null };
  }
  return JSON.parse(el.textContent) as PageData;
}
