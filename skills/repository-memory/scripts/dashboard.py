#!/usr/bin/env python3
"""Read-only, zero-dependency dashboard for Repository Memory."""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>记忆基地</title><style>
:root{--paper:#f3efe5;--ink:#17201b;--muted:#667168;--line:#cfd4c9;--green:#174f3b;--mint:#dceade;--amber:#a76021;--white:#fffdf7}*{box-sizing:border-box}body{font:15px/1.55 system-ui,-apple-system,"PingFang SC",sans-serif;background:var(--paper);color:var(--ink);margin:0}main{max-width:1180px;margin:auto;padding:38px 24px 70px}.eyebrow{font:700 12px/1.2 ui-monospace,monospace;letter-spacing:.13em;text-transform:uppercase;color:var(--green)}h1{font:700 clamp(36px,7vw,76px)/.98 Georgia,"Songti SC",serif;max-width:800px;margin:14px 0 20px;letter-spacing:-.04em}.lead{font-size:18px;max-width:720px;color:#48534b;margin:0 0 32px}.statusline{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:30px}.pill{border:1px solid var(--line);background:#ffffff73;padding:6px 11px;border-radius:999px;font-size:13px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#859087;margin-right:7px}.ready .dot{background:#2d8b57}.stats{display:grid;grid-template-columns:repeat(4,1fr);border-block:1px solid var(--ink);margin:26px 0 42px}.stat{padding:20px 16px;border-right:1px solid var(--line)}.stat:last-child{border:0}.num{font:700 34px/1 ui-monospace,monospace}.label,.muted{color:var(--muted);font-size:13px}h2{font:700 25px/1.15 Georgia,"Songti SC",serif;margin:0 0 14px}.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px}.panel{background:var(--white);border:1px solid var(--line);padding:24px}.panel.dark{background:var(--green);color:#f6f4e9;border-color:var(--green)}.panel.dark .muted{color:#bdd0c4}.jobs{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:18px}.job{background:var(--white);padding:18px}.job b{display:block;margin-bottom:5px}.job span{color:var(--muted);font-size:13px}.bar{display:flex;gap:8px;margin:16px 0 10px}.bar input{min-width:0;flex:1;background:#fff;border:1px solid #879288;padding:12px 13px}.bar button,.preset{border:1px solid var(--green);background:var(--green);color:#fff;padding:10px 14px;font-weight:700;cursor:pointer}.presets{display:flex;flex-wrap:wrap;gap:7px}.preset{background:transparent;color:var(--green);font-size:12px;padding:6px 9px}.verdict{padding:13px 14px;margin:15px 0;background:var(--mint);border-left:4px solid var(--green)}.verdict.abstain{background:#f4e5d5;border-color:var(--amber)}.result{border-top:1px solid var(--line);padding:15px 0}.result p{margin:6px 0}.meta{font-size:12px;color:var(--muted);word-break:break-all}.sectionlabel{font:700 11px ui-monospace,monospace;letter-spacing:.1em;color:var(--muted);margin:18px 0 5px}.layers{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0}.layer{border:1px solid #ffffff40;padding:14px 10px}.layer .num{font-size:25px}.note{font-size:13px;border-top:1px solid #ffffff40;padding-top:16px}.footer{margin-top:20px;color:var(--muted);font-size:12px}@media(max-width:800px){.stats{grid-template-columns:1fr 1fr}.stat:nth-child(2){border-right:0}.grid,.jobs{grid-template-columns:1fr}main{padding:26px 16px}.num{font-size:28px}}
</style></head><body><main><div class="eyebrow">Repository Memory / Live Evidence</div><h1>这套记忆系统，<br>到底替你记住什么？</h1><p class="lead">它不是“多存聊天记录”。它把项目事实、对话经验和团队结论分层保存，并要求答案带来源；没有足够证据时明确拒答。</p><div class="statusline"><span class="pill" id="runtime"><i class="dot"></i>正在读取真实运行状态</span><span class="pill" id="repo">仓库：—</span><span class="pill">只读演示 · 不改任何记忆</span></div>
<div class="stats"><div class="stat"><div class="num" id="sources">—</div><div class="label">已配置知识源</div></div><div class="stat"><div class="num" id="active">—</div><div class="label">活跃团队记忆</div></div><div class="stat"><div class="num" id="candidate">—</div><div class="label">待审核候选</div></div><div class="stat"><div class="num" id="revisions">—</div><div class="label">可追踪历史版本</div></div></div>
<div class="jobs"><div class="job"><b>01 找回项目事实</b><span>从真实文件和提交定位答案，并返回行号证据。</span></div><div class="job"><b>02 延续跨轮上下文</b><span>L0 原始记录 → L1 事实 → L2 场景 → L3 方法论。</span></div><div class="job"><b>03 共享团队经验</b><span>候选先审核，再成为其他 Agent 可复用的结论。</span></div><div class="job"><b>04 控制幻觉</b><span>检索命中不等于能回答；支持度不足就明确拒答。</span></div></div>
<div class="grid"><section class="panel"><h2>亲手试一次</h2><div class="muted">结果严格分成“足以回答的证据”和“仅供调查的线索”。</div><div class="bar"><input id="q" value="为什么 team candidates 不能直接当事实？"><button onclick="runSearch()">查记忆</button></div><div class="presets"><button class="preset" onclick="ask('为什么 team candidates 不能直接当事实？')">查一条已知规则</button><button class="preset" onclick="ask('火星香蕉协议的管理员是谁？')">试一个不存在的问题</button></div><div id="results"><div class="verdict">点击“查记忆”查看真实返回。</div></div></section>
<section class="panel dark"><div class="eyebrow" style="color:#b9dbc8">Memory Layers</div><h2>从记录到可复用经验</h2><div class="layers"><div class="layer"><div class="num" id="l0">—</div><div class="muted">L0 记录</div></div><div class="layer"><div class="num" id="l1">—</div><div class="muted">L1 事实</div></div><div class="layer"><div class="num" id="l2">—</div><div class="muted">L2 场景</div></div><div class="layer"><div class="num" id="l3">—</div><div class="muted">L3 方法</div></div></div><p class="note"><b>这次刚加上的质量控制</b><br><span class="muted">“收到、MR 已开”这类低信息确认不再进入候选池；中央包装同一来源时按谱系折叠，不再制造双份记忆。</span></p><p class="note"><b>效果应该怎么看</b><br><span class="muted">这里展示实时规模、证据和拒答行为，不冒充排行榜分数。检索精度另由固定查询集衡量。</span></p></section></div><div class="footer" id="foot">数据来自本机运行时与当前 Git 工作树。</div>
<script>const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function get(p){const r=await fetch(p),x=await r.json();if(!r.ok)throw Error(x.error||r.status);return x}function pop(x){return x?.record_count??x?.population?.count??x?.count??0}async function load(){const x=await get('/api/overview');$('#runtime').classList.add('ready');$('#runtime').innerHTML='<i class="dot"></i>'+(x.runtime_status==='ready'?'运行正常':esc(x.runtime_status));$('#repo').textContent='仓库：'+(x.repository||'未配置');$('#sources').textContent=x.source_count??0;$('#active').textContent=x.team?.by_status?.active??0;$('#candidate').textContent=x.team?.by_status?.candidate??0;$('#revisions').textContent=x.team?.revision_count??0;for(const k of ['l0','l1','l2','l3'])$('#'+k).textContent=pop(x.memory?.layers?.[k.toUpperCase()]||x.memory?.layers?.[k]||{});$('#foot').textContent='实时读取：'+new Date().toLocaleString()+' · '+(x.root||'')}function ask(q){$('#q').value=q;runSearch()}function card(r){const c=r.citation||{},l=c.locator||{},lines=l.start_line?':'+l.start_line+(l.end_line?'–'+l.end_line:''):'';return `<article class="result"><b>${esc(r.title||r.path||r.id)}</b><p>${esc(r.excerpt||r.summary||'')}</p><div class="meta"><code>${esc(c.path||r.path||'')}${esc(lines)}</code> · ${esc(c.commit||'未固定提交')}</div></article>`}async function runSearch(){const q=$('#q').value.trim();if(!q)return;$('#results').innerHTML='<div class="verdict">正在核对证据…</div>';try{const x=await get('/api/search?q='+encodeURIComponent(q)),a=x.answerable||[],leads=(x.verified||[]).filter(v=>!a.some(y=>(y.id||y.path)===(v.id||v.path))).slice(0,3);let h=a.length?`<div class="verdict"><b>可以回答</b>：找到 ${a.length} 条直接支持问题的证据。</div>`:`<div class="verdict abstain"><b>选择拒答</b>：可能找到相关文件，但没有内容足以直接支持这个问题。</div>`;if(a.length)h+='<div class="sectionlabel">ANSWERABLE EVIDENCE</div>'+a.map(card).join('');if(leads.length)h+='<div class="sectionlabel">INVESTIGATIVE LEADS — NOT FACTS</div>'+leads.map(card).join('');$('#results').innerHTML=h}catch(e){$('#results').innerHTML='<div class="verdict abstain">读取失败：'+esc(e.message)+'</div>'}}load().catch(e=>$('#runtime').innerHTML='<i class="dot"></i>诊断失败：'+esc(e.message));runSearch();</script></main></body></html>'''


def dashboard_overview(root: Path) -> dict[str, Any]:
    """Compose live read-only state without inventing an effectiveness score."""
    from core import doctor
    from memorycore import native_memory_client
    from team_memory import team_memory_store
    diagnosis = doctor(root)
    memory_health = native_memory_client().health(refresh=True, probe_layers=True)
    team_health = team_memory_store().health()
    layers = {name: {"record_count": value.get("record_count", 0), "status": value.get("api_status")}
              for name, value in (memory_health.get("layers") or {}).items()}
    team = {key: team_health.get(key) for key in ("status", "reachable", "by_status", "revision_count")}
    repository = diagnosis.get("repository")
    repository_name = repository if isinstance(repository, str) else root.name
    ready = diagnosis.get("status") == "ready" and memory_health.get("status") == "ready" and team_health.get("reachable") is True
    return {"ok": True, "repository": repository_name,
            "runtime_status": "ready" if ready else "degraded",
            "source_count": diagnosis.get("source_count", len(diagnosis.get("sources") or [])),
            "memory": {"layers": layers}, "team": team, "read_only": True, "canonical_repo_changed": False}


def serve_dashboard(root: Path | None, *, host: str = "127.0.0.1", port: int = 0, open_window: bool = False) -> dict[str, Any]:
    from discovery import resolve_root
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("the built-in dashboard only binds to a loopback host; use an authenticated proxy for remote access")
    resolved = root.expanduser().resolve() if root else resolve_root(None)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: dict[str, Any], status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    payload = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
                if parsed.path == "/api/overview": self._json(dashboard_overview(resolved)); return
                if parsed.path == "/api/search":
                    from core import search
                    query = str(parse_qs(parsed.query).get("q", [""])[0]).strip()
                    if not query: self._json({"ok": False, "error": "query is required"}, 400); return
                    self._json(search(resolved, query, 8, False, None, True, "repository")); return
                self._json({"ok": False, "error": "not found"}, 404)
            except Exception as exc:
                del exc
                self._json({"ok": False, "error": "dashboard request failed; inspect the local process log"}, 500)
        def log_message(self, *_args: Any) -> None: return

    server = ThreadingHTTPServer((host, int(port)), Handler); url = f"http://{host}:{server.server_port}/"
    if open_window: threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    print(json.dumps({"ok": True, "url": url, "read_only": True, "canonical_repo_changed": False}, ensure_ascii=False), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return {"ok": True, "url": url, "stopped": True, "canonical_repo_changed": False}
