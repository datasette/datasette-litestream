"""
Minimal Vite manifest helper for serving a built frontend bundle.

This is a small self-contained reimplementation of the ``datasette-vite``
``vite_entry`` helper (that package is not yet published to PyPI). It reads the
``manifest.json`` that ``vite build`` writes into the plugin package and returns
the ``<script type=module>`` / ``<link rel=stylesheet>`` tags for an entry
point, resolving hashed filenames and transitively-imported CSS.

Built assets live under ``datasette_litestream/static/gen/`` and are served by
Datasette at ``/-/static-plugins/datasette_litestream/...`` (the ``static/``
prefix is stripped). In development you can instead point at a running Vite dev
server (HMR) by setting ``vite_dev_path`` in plugin config or the
``DATASETTE_LITESTREAM_VITE_PATH`` environment variable.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

from .config import get_config

PLUGIN_PACKAGE = "datasette_litestream"


@lru_cache(maxsize=1)
def _manifest_path() -> Path:
    return Path(__file__).resolve().parent / "manifest.json"


def _load_manifest() -> dict:
    path = _manifest_path()
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _vite_dev_path(datasette) -> str | None:
    """Return the Vite dev server base URL (with trailing slash), if configured."""
    env = os.environ.get("DATASETTE_LITESTREAM_VITE_PATH")
    if env:
        return env if env.endswith("/") else env + "/"
    dev = get_config(datasette).vite_dev_path
    if dev:
        return dev if dev.endswith("/") else dev + "/"
    return None


def _asset_url(datasette, file: str) -> str:
    # Manifest paths look like "static/gen/main-HASH.js"; Datasette serves the
    # package's static/ directory at /-/static-plugins/<package>/, so strip it.
    rel = file[len("static/") :] if file.startswith("static/") else file
    return datasette.urls.static_plugins(PLUGIN_PACKAGE, rel)


def _collect_css(manifest: dict, key: str, seen: set) -> list:
    """Collect CSS files for an entry plus its transitive imports."""
    if key in seen or key not in manifest:
        return []
    seen.add(key)
    chunk = manifest[key]
    css = list(chunk.get("css", []) or [])
    for imported in chunk.get("imports", []) or []:
        css.extend(_collect_css(manifest, imported, seen))
    return css


def vite_entry(datasette, entrypoint: str) -> str:
    """Return the HTML head tags that load ``entrypoint`` (e.g. ``src/main.ts``)."""
    dev = _vite_dev_path(datasette)
    if dev:
        # Dev mode: load Vite's HMR client and the raw source entry.
        return (
            f'<script type="module" src="{dev}@vite/client"></script>\n'
            f'<script type="module" src="{dev}{entrypoint}"></script>'
        )

    manifest = _load_manifest()
    chunk = manifest.get(entrypoint)
    if chunk is None:
        return (
            f"<!-- datasette-litestream: vite entry {entrypoint!r} not found in "
            f"manifest; run the frontend build -->"
        )

    tags = []
    for css_file in _collect_css(manifest, entrypoint, set()):
        tags.append(f'<link rel="stylesheet" href="{_asset_url(datasette, css_file)}">')
    tags.append(
        f'<script type="module" src="{_asset_url(datasette, chunk["file"])}"></script>'
    )
    return "\n".join(tags)
