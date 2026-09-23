"""Dependency-free local history UI and JSON API."""

# The embedded HTML, CSS and JavaScript retain their native formatting.
# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from kubeproof.history import HistoryError, HistoryService

APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KubeProof History</title>
  <style>
    :root { color-scheme: dark; --bg:#09111f; --panel:#111c2e; --line:#26344b;
      --text:#ecf3ff; --muted:#91a0b8; --good:#57d49b; --bad:#ff6b7a;
      --warn:#f3b957; --cold:#65a9ff; }
    * { box-sizing:border-box; }
    body { margin:0; background:radial-gradient(circle at top left,#162944 0,var(--bg) 38%);
      color:var(--text); font:14px/1.5 ui-sans-serif,system-ui,sans-serif; min-height:100vh; }
    main { max-width:1240px; margin:auto; padding:36px 24px 72px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; }
    h1 { margin:0; font-size:30px; letter-spacing:-.04em; }
    h2,h3 { margin:0; } p { margin:.35rem 0; }
    .eyebrow { color:var(--cold); font-size:12px; font-weight:800; letter-spacing:.16em;
      text-transform:uppercase; }
    button { border:1px solid var(--line); border-radius:9px; padding:9px 13px;
      color:var(--text); background:#17253a; cursor:pointer; font-weight:700; }
    button:hover { border-color:var(--cold); }
    .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:26px 0; }
    .stat,.panel { border:1px solid var(--line); background:color-mix(in srgb,var(--panel) 93%,transparent);
      box-shadow:0 16px 45px #0004; border-radius:13px; }
    .stat { padding:15px 17px; } .stat span { display:block; color:var(--muted); font-size:12px; }
    .stat strong { display:block; font-size:24px; margin-top:2px; }
    .panel { overflow:hidden; }
    table { width:100%; border-collapse:collapse; }
    th { color:var(--muted); text-align:left; font-size:11px; text-transform:uppercase;
      letter-spacing:.09em; background:#0d1727; }
    th,td { padding:13px 15px; border-bottom:1px solid var(--line); }
    tbody tr { cursor:pointer; } tbody tr:hover { background:#18273c; }
    tbody tr:last-child td { border-bottom:0; }
    .muted { color:var(--muted); } .mono { font-family:ui-monospace,SFMono-Regular,monospace; }
    .badge { display:inline-flex; align-items:center; border:1px solid currentColor; border-radius:999px;
      padding:2px 8px; font-size:11px; font-weight:800; text-transform:uppercase; }
    .pass,.admit { color:var(--good); } .fail,.blocker,.missing { color:var(--bad); }
    .warning { color:var(--warn); } .not_tested,.not_applicable,.inconclusive { color:var(--muted); }
    dialog { width:min(900px,calc(100vw - 28px)); max-height:90vh; padding:0; color:var(--text);
      border:1px solid var(--line); border-radius:15px; background:var(--bg); box-shadow:0 30px 90px #000b; }
    dialog::backdrop { background:#020713cc; backdrop-filter:blur(4px); }
    .dialog-head { position:sticky; top:0; z-index:1; display:flex; justify-content:space-between;
      gap:20px; padding:20px 22px; background:#0d1727; border-bottom:1px solid var(--line); }
    .dialog-body { padding:20px 22px 28px; }
    .check { border:1px solid var(--line); border-radius:11px; padding:15px; margin:12px 0; }
    .check-head { display:flex; justify-content:space-between; gap:15px; }
    .finding { border-left:3px solid var(--warn); padding:8px 12px; margin:12px 0 4px; background:#141f31; }
    .finding.blocker { border-color:var(--bad); color:var(--text); }
    .evidence { margin:8px 0 0 18px; padding:0; } pre { white-space:pre-wrap; word-break:break-word;
      color:#bed3ee; background:#0a1322; padding:10px; border-radius:8px; }
    .empty,.error { padding:32px; text-align:center; color:var(--muted); }
    .error { color:var(--bad); }
    @media (max-width:760px) { .stats { grid-template-columns:repeat(2,1fr); }
      th:nth-child(2),td:nth-child(2),th:nth-child(4),td:nth-child(4) { display:none; } }
  </style>
</head>
<body><main>
  <header><div><div class="eyebrow">Local evidence explorer</div><h1>KubeProof History</h1>
    <p class="muted">Evaluation outcomes, findings and attributable observations.</p></div>
    <button id="sync">Resync bundles</button></header>
  <section class="stats">
    <div class="stat"><span>Evaluations</span><strong id="total">—</strong></div>
    <div class="stat"><span>With blockers</span><strong id="blocked">—</strong></div>
    <div class="stat"><span>Warnings</span><strong id="warnings">—</strong></div>
    <div class="stat"><span>Missing bundles</span><strong id="missing">—</strong></div>
  </section>
  <section class="panel"><table><thead><tr><th>Generated</th><th>Chart</th><th>Profile</th>
    <th>Admission</th><th>Findings</th></tr></thead><tbody id="rows"></tbody></table>
    <div class="empty" id="empty" hidden>No evaluations indexed yet.</div></section>
</main>
<dialog id="detail"><div class="dialog-head"><div><div class="eyebrow">Evaluation detail</div>
  <h2 id="detail-title">Loading…</h2></div><button id="close">Close</button></div>
  <div class="dialog-body" id="detail-body"></div></dialog>
<script>
  const esc = value => String(value ?? '');
  const badge = value => `<span class="badge ${esc(value)}">${esc(value).replaceAll('_',' ')}</span>`;
  const rows = document.querySelector('#rows');
  const detail = document.querySelector('#detail');

  function node(tag, text, className='') {
    const element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className; return element;
  }

  async function loadList() {
    rows.replaceChildren(); document.querySelector('#empty').hidden = true;
    const response = await fetch('/api/evaluations');
    if (!response.ok) throw new Error(await response.text());
    const items = await response.json();
    document.querySelector('#total').textContent = items.length;
    document.querySelector('#blocked').textContent = items.filter(x => x.blocker_count).length;
    document.querySelector('#warnings').textContent = items.reduce((n,x) => n+x.warning_count,0);
    document.querySelector('#missing').textContent = items.filter(x => x.bundle_status==='missing').length;
    document.querySelector('#empty').hidden = items.length !== 0;
    for (const item of items) {
      const tr = document.createElement('tr');
      const date = new Date(item.generated_at).toLocaleString();
      tr.append(node('td',date)); tr.append(node('td',item.chart,'mono'));
      tr.append(node('td',item.profile_name));
      const admission = document.createElement('td'); admission.innerHTML=badge(item.admission_outcome); tr.append(admission);
      const findings = document.createElement('td');
      findings.textContent = `${item.blocker_count} blocker · ${item.warning_count} warning`;
      if (item.bundle_status === 'missing') findings.append(' ', node('span','missing bundle','badge missing'));
      tr.append(findings); tr.addEventListener('click',()=>openDetail(item.evaluation_id)); rows.append(tr);
    }
  }

  function observationText(observation) {
    const resource = observation.resource;
    const subject = resource ? `${resource.kind} ${resource.namespace ? resource.namespace+'/' : ''}${resource.name}` : 'evaluation';
    return `${observation.id} · ${subject} — ${observation.summary}`;
  }

  async function openDetail(id) {
    detail.showModal(); document.querySelector('#detail-title').textContent=id;
    const body=document.querySelector('#detail-body'); body.replaceChildren(node('p','Loading…','muted'));
    const response=await fetch(`/api/evaluations/${encodeURIComponent(id)}`);
    if (!response.ok) { body.replaceChildren(node('p',await response.text(),'error')); return; }
    const evaluation=await response.json();
    document.querySelector('#detail-title').textContent=evaluation.input.chart;
    body.replaceChildren();
    body.append(node('p',`${evaluation.profile_name} · ${evaluation.generated_at} · ${evaluation.evaluation_id}`,'muted mono'));
    const findingById=Object.fromEntries(evaluation.findings.map(x=>[x.id,x]));
    const observationById=Object.fromEntries(evaluation.observations.map(x=>[x.id,x]));
    for (const check of evaluation.checks) {
      const card=document.createElement('section'); card.className='check';
      const head=document.createElement('div'); head.className='check-head';
      const title=document.createElement('div'); title.append(node('h3',check.title));
      title.append(node('p',`${check.id} · execution: ${check.execution_status}`,'muted mono'));
      head.append(title); const mark=document.createElement('div'); mark.innerHTML=badge(check.assessment); head.append(mark); card.append(head);
      if (check.explanation) card.append(node('p',check.explanation,'muted'));
      for (const findingId of check.finding_ids) {
        const finding=findingById[findingId]; if (!finding) continue;
        const box=document.createElement('div'); box.className=`finding ${finding.severity}`;
        box.append(node('strong',`[${finding.severity}] ${finding.title}`)); box.append(node('p',finding.description));
        if (finding.constraint) box.append(node('p',`Constraint: ${finding.constraint}`,'muted mono'));
        if (finding.limitation) box.append(node('p',`Limitation: ${finding.limitation}`,'muted'));
        card.append(box);
      }
      const evidenceIds=[...new Set(check.observation_ids)];
      if (evidenceIds.length) {
        card.append(node('p','Evidence','eyebrow')); const list=document.createElement('ul'); list.className='evidence';
        for (const observationId of evidenceIds) { const observation=observationById[observationId]; if (!observation) continue;
          const item=document.createElement('li'); item.append(node('div',observationText(observation)));
          if (Object.keys(observation.data).length) item.append(node('pre',JSON.stringify(observation.data,null,2))); list.append(item); }
        card.append(list);
      } else if (!check.finding_ids.length) card.append(node('p','No findings or observations recorded.','muted'));
      body.append(card);
    }
  }

  document.querySelector('#close').addEventListener('click',()=>detail.close());
  document.querySelector('#sync').addEventListener('click',async event=>{
    event.target.disabled=true; try { const response=await fetch('/api/sync',{method:'POST'});
      if (!response.ok) throw new Error(await response.text()); await loadList(); }
    catch (error) { alert(error.message); } finally { event.target.disabled=false; }
  });
  loadList().catch(error=>{ document.querySelector('#empty').hidden=false;
    document.querySelector('#empty').textContent=error.message; document.querySelector('#empty').className='error'; });
</script></body></html>"""


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def make_handler(service: HistoryService) -> type[BaseHTTPRequestHandler]:
    class HistoryHandler(BaseHTTPRequestHandler):
        def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._respond(status, _json_bytes({"error": message}), "application/json")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self._respond(HTTPStatus.OK, APP_HTML.encode(), "text/html; charset=utf-8")
                    return
                if path == "/api/evaluations":
                    payload = [asdict(item) for item in service.list_evaluations()]
                    self._respond(HTTPStatus.OK, _json_bytes(payload), "application/json")
                    return
                prefix = "/api/evaluations/"
                if path.startswith(prefix):
                    evaluation_id = unquote(path[len(prefix) :])
                    evaluation = service.get_evaluation(evaluation_id)
                    if evaluation is None:
                        self._error(HTTPStatus.NOT_FOUND, "evaluation not found")
                        return
                    self._respond(
                        HTTPStatus.OK,
                        _json_bytes(evaluation.model_dump(mode="json")),
                        "application/json",
                    )
                    return
                self._error(HTTPStatus.NOT_FOUND, "not found")
            except HistoryError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/sync":
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                count = service.sync()
            except HistoryError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
                return
            self._respond(HTTPStatus.OK, _json_bytes({"indexed": count}), "application/json")

    return HistoryHandler


def serve_history(service: HistoryService, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        server.server_close()
