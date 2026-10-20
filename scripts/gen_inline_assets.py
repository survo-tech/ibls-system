#!/usr/bin/env python3
"""Generate build/inline/assets.ts (site/ files as base64) for wrangler.inline.toml.
Used when the assets upload API is unavailable (e.g. a proxied token); CI uses wrangler.toml."""
import base64, json, mimetypes, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
site = root / "site"
out = root / "build" / "inline"; out.mkdir(parents=True, exist_ok=True)
types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8"}
m = {}
for f in sorted(site.rglob("*")):
    if f.is_file() and f.name != "_headers":
        rel = "/" + f.relative_to(site).as_posix()
        m[rel] = {"t": types.get(f.suffix, mimetypes.guess_type(f.name)[0] or "application/octet-stream"),
                  "b": base64.b64encode(f.read_bytes()).decode()}
(out / "assets.ts").write_text("export const ASSETS_MAP: Record<string,{t:string,b:string}> = " + json.dumps(m) + ";\n")
print(len(m), "files")
