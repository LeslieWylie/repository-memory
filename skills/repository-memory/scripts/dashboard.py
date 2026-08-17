#!/usr/bin/env python3
"""Small read-only local dashboard for Repository Memory.

It deliberately uses only the Python standard library.  It is a viewer for
doctor/search/memory state, not a second retrieval implementation and not a
write surface.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repository Memory</title><style>
body{font:14px system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;margin:0}main{max-width:1100px;margin:0 auto;padding:28px}
h1{margin:0 0 8px}p{color:#94a3b8}.bar{display:flex;gap:8px;margin:20px 0}.bar input{flex:1;background:#1e293b;border:1px solid #475569;color:#fff;border-radius:8px;padding:11px}.bar button{background:#38bdf8;border:0;border-radius:8px;padding:0 18px;font-weight:700}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{background:#111827;border:1px solid #334155;border-radius:12px;padding:16px}.wide{grid-column:1/-1}pre{white-space:pre-wrap;overflow:auto;color:#cbd5e1}.ok{color:#34d399}.warn{color:#fbbf24}.result{border-top:1px solid #334155;padding:12px 0}.result:first-child{border-top:0}.muted{color:#94a3b8;font-size:12px}
@media(max-width:760px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style></head><body><main><h1>Repository Memory</h1><p>Read-only local dashboard. Git remains the source of truth.</p>
<div class="bar"><input id="q" placeholder="Ask a repository question"><button onclick="search()">Search</button></div>
<div class="grid"><section class="card"><h2>Doctor</h2><pre id="doctor">Loading...</pre></section><section class="card"><h2>Memory layers</h2><pre id="memory">Loading...</pre></section><section class="card wide"><h2>Results</h2><div id="results" class="muted">Run a search.</div></section></div>
<script>
const pretty=x=>JSON.stringify(x,null,2);
async function get(path){const r=await fetch(path);return await r.json()}
async function load(){const d=await get('/api/doctor');document.querySelector('#doctor').textContent=pretty({status:d.status,source_count:d.source_count,sources:d.sources,routing:d.routing,index:d.index});const m=await get('/api/memory');document.querySelector('#memory').textContent=pretty({status:m.status,layers:m.layers,embedding:m.embedding});}
async function search(){const q=document.querySelector('#q').value.trim();if(!q)return;const x=await get('/api/search?q='+encodeURIComponent(q));const box=document.querySelector('#results');box.innerHTML=(x.verified||[]).map(r=>`<div class="result"><b>${r.title||r.path||r.id}</b><div>${(r.excerpt||'').replaceAll('&','&amp;').replaceAll('<','&lt;')}</div><div class="muted">${r.citation||r.path||''} · ${r.freshness?.state||r.freshness||''} · ${r.status||r.evidence_status||''}</div></div>`).join('')||'<span class="warn">No verified result; abstain.</span>';}
load().catch(e=>document.querySelector('#doctor').textContent=String(e));
</script></main></body></html>"""


def serve_dashboard(root: Path | None, *, host: str = "127.0.0.1", port: int = 0, open_window: bool = False) -> dict[str, Any]:
    root_value = str(root) if root else None

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: dict[str, Any], status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                from core import doctor, search
                from memorycore import native_memory_client
                from discovery import resolve_root

                resolved = Path(root_value).expanduser().resolve() if root_value else resolve_root(None)
                if parsed.path == "/":
                    payload = HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if parsed.path == "/api/doctor":
                    self._json(doctor(resolved))
                    return
                if parsed.path == "/api/memory":
                    self._json(native_memory_client().health(refresh=True, probe_layers=True))
                    return
                if parsed.path == "/api/search":
                    query = str(parse_qs(parsed.query).get("q", [""])[0])
                    self._json(search(resolved, query, 8, False, None, False, "repository"))
                    return
                self._json({"ok": False, "error": "not found"}, 404)
            except Exception as exc:  # dashboard must show diagnosis, never fake data
                self._json({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}, 500)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    url = f"http://{host}:{server.server_port}/"
    if open_window:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    print(json.dumps({"ok": True, "url": url, "read_only": True, "canonical_repo_changed": False}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"ok": True, "url": url, "stopped": True, "canonical_repo_changed": False}
