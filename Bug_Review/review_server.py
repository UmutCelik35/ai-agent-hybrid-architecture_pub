from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Bug_Review.bug_review import create_bug_from_candidate, iter_pending_bug_candidates
from Resources.config import Config
from Resources.logger_config import get_logger
from Static_Aut.reporting.html_reporter import write_report_files


logger = get_logger("BugReviewServer")


class ReviewApp:
    # Small application layer around the report file. The HTTP handler delegates
    # report reads, writes, and approval actions here.
    def __init__(self, report_json_path: Path):
        self.report_json_path = report_json_path

    def load(self) -> dict[str, Any]:
        # Always read the current report from disk so browser refreshes see the
        # latest approval state.
        return json.loads(self.report_json_path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> None:
        payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        report_html_path = self.report_json_path.with_suffix(".html")
        # Rewrite the existing report after review actions so approved bugs,
        # Jira issue keys, and updated statuses are persisted in both JSON and HTML.
        write_report_files(payload, report_html_path, self.report_json_path)

        for result in payload.get("test_results") or []:
            result_json_path = str(result.get("result_json_path") or "").strip()
            if not result_json_path:
                continue
            full_path = Config.PROJECT_ROOT / result_json_path
            result_payload = dict(result)
            if full_path.exists():
                try:
                    # Preserve fields that may exist only in the single-test JSON
                    # while overlaying the updated bug state from the execution report.
                    current = json.loads(full_path.read_text(encoding="utf-8"))
                    current.update(result_payload)
                    result_payload = current
                except Exception:
                    pass
            full_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def approve(self, candidate_ids: list[str]) -> dict[str, Any]:
        # Approving a candidate is the only place where the review server creates
        # Jira issues; unselected candidates remain pending in the report.
        payload = self.load()
        results = []

        for result in payload.get("test_results") or []:
            bug = result.get("bug") or {}
            candidate_id = str(bug.get("id") or "")
            if candidate_id not in candidate_ids:
                continue

            if bug.get("created"):
                results.append({"id": candidate_id, "status": "already_created", "key": bug.get("key", "")})
                continue

            updated_bug = create_bug_from_candidate(bug)
            result["bug"] = updated_bug
            results.append(
                {
                    "id": candidate_id,
                    "status": updated_bug.get("status"),
                    "key": updated_bug.get("key", ""),
                    "error": updated_bug.get("error", ""),
                }
            )

        payload["summary"] = _summary(payload.get("test_results") or [])
        self.save(payload)
        return {"results": results, "report": _review_payload(payload, self.report_json_path)}


class ReviewHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler creates one handler instance per request; the shared
    # ReviewApp instance is attached by main() before the server starts.
    app: ReviewApp | None = None

    def do_GET(self):
        # The browser loads static HTML first, then calls /api/report for data.
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(_render_page())
            return
        if parsed.path == "/api/report":
            self._send_json(_review_payload(self.app.load(), self.app.report_json_path))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        # The UI posts selected candidate IDs here when the user confirms creation.
        parsed = urlparse(self.path)
        if parsed.path != "/api/approve":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return

        candidate_ids = [str(item).strip() for item in body.get("candidate_ids") or [] if str(item).strip()]
        if not candidate_ids:
            self.send_error(HTTPStatus.BAD_REQUEST, "candidate_ids is required")
            return

        response = self.app.approve(candidate_ids)
        self._send_json(response)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    # The launcher passes the report JSON path, but this file can also be run
    # directly for debugging a specific execution report.
    args = parse_args()
    report_json_path = Path(args.report_json).resolve()
    if not report_json_path.exists():
        raise SystemExit(f"Report JSON not found: {report_json_path}")

    app = ReviewApp(report_json_path)
    ReviewHandler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    host, port = server.server_address
    review_url = f"http://{host}:{port}/"

    logger.info("Bug review server listening on %s", review_url)
    if args.open_browser:
        webbrowser.open(review_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Bug review server stopped by user.")
    finally:
        server.server_close()
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Run the local bug review server")
    parser.add_argument("--report-json", required=True, help="Path to an execution report JSON file")
    parser.add_argument("--open-browser", action="store_true", help="Open the review UI in the default browser")
    parser.add_argument("--port", type=int, default=0, help="Port to bind. Use 0 for an ephemeral port.")
    return parser.parse_args()


def _review_payload(payload: dict[str, Any], report_json_path: Path) -> dict[str, Any]:
    # Convert the full execution report into the smaller shape consumed by the
    # browser UI.
    execution_key = payload.get("execution_key") or "execution"
    report_html_path = report_json_path.with_suffix(".html")
    candidates = []
    for result in payload.get("test_results") or []:
        bug = result.get("bug") or {}
        # The review screen only lists pending candidates and already-created bugs.
        if not bug.get("candidate") and not bug.get("created"):
            continue
        entry = {
            "id": bug.get("id", ""),
            "created": bool(bug.get("created")),
            "key": bug.get("key", ""),
            "status": bug.get("status", ""),
            "approved": bool(bug.get("approved")),
            "source": bug.get("source", ""),
            "project_key": bug.get("project_key", ""),
            "xray_test_key": bug.get("xray_test_key", result.get("test_key", "")),
            "summary": bug.get("summary", ""),
            "test_summary": bug.get("test_summary", result.get("test_summary", "")),
            "failed_step_no": bug.get("failed_step_no"),
            "failed_step_text": bug.get("failed_step_text", ""),
            "expected_result": bug.get("expected_result", ""),
            "actual_result": bug.get("actual_result", ""),
            "page_url": bug.get("page_url", ""),
            "screenshot_path": bug.get("screenshot_path", ""),
            "error_message": bug.get("error_message", ""),
            "unsupported": _unsupported_text(result),
            "mcp_used": _mcp_used_text(result),
            "tool_suggestion": _tool_suggestion_text(result),
        }
        candidates.append(entry)

    return {
        "execution_key": execution_key,
        "started_at_utc": payload.get("started_at_utc", ""),
        "finished_at_utc": payload.get("finished_at_utc", ""),
        "summary": payload.get("summary") or _summary(payload.get("test_results") or []),
        "report_json_path": _relative(report_json_path),
        "report_html_path": _relative(report_html_path),
        "report_html_absolute_path": str(report_html_path.resolve()),
        "candidates": candidates,
        "pending_count": len(iter_pending_bug_candidates(payload)),
    }


def _summary(test_results: list[dict[str, Any]]) -> dict[str, int]:
    # Recompute counts after approvals so the page and HTML report stay in sync.
    return {
        "total": len(test_results),
        "passed": sum(1 for result in test_results if str(result.get("final_status", "")).startswith("passed")),
        "failed": sum(1 for result in test_results if result.get("return_code") and "unsupported" not in str(result.get("final_status", ""))),
        "unsupported": sum(1 for result in test_results if "unsupported" in str(result.get("final_status", ""))),
        "tool_suggestions": sum(1 for result in test_results if result.get("tool_suggestion", {}).get("created")),
        "mcp_healing": sum(
            1
            for result in test_results
            if result.get("static", {}).get("healing_attempted") or result.get("mcp_full_scenario_used")
        ),
        "bugs": sum(1 for result in test_results if result.get("bug", {}).get("created")),
        "bug_candidates": sum(1 for result in test_results if result.get("bug", {}).get("candidate")),
    }


def _relative(path: Path) -> str:
    try:
        return path.relative_to(Config.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _unsupported_text(result: dict[str, Any]) -> str:
    # Expected Result gaps and unsupported action steps are displayed differently
    # from normal failed assertions.
    if result.get("non_executable_expected_result"):
        return "expected result"
    steps = result.get("static", {}).get("unsupported_steps") or []
    texts = [
        str(step.get("text", "")).strip()
        for step in steps
        if str(step.get("text", "")).strip()
    ]
    return "; ".join(texts) if texts else "no"


def _mcp_used_text(result: dict[str, Any]) -> str:
    # Show whether MCP was used for full execution or only for static locator healing.
    if result.get("mcp_full_scenario_used"):
        return "full scenario"
    static = result.get("static", {}) or {}
    if not static.get("healing_attempted"):
        return "not started"
    patch = static.get("healing_patch") or {}
    if patch.get("selector"):
        return f"started | {patch['selector']}"
    if patch.get("error"):
        return f"failed | {patch['error']}"
    return "started | no locator patch"


def _tool_suggestion_text(result: dict[str, Any]) -> str:
    suggestion = result.get("tool_suggestion") or {}
    if not suggestion.get("created"):
        return "no"
    path = str(suggestion.get("path") or "").strip()
    content = suggestion.get("content") or {}
    if isinstance(content, dict):
        suggested_tools = content.get("suggestions") or content.get("suggested_tools") or content.get("tools") or []
        if isinstance(suggested_tools, list) and suggested_tools:
            count_text = f"{len(suggested_tools)} suggested tool(s)"
            return f"{count_text} | {path}" if path else count_text
    return path or "yes"


def _render_page() -> str:
    # The review UI is intentionally embedded so the server has no static asset
    # dependency and can run from any generated report path.
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bug Review</title>
  <style>
    :root {
      --bg: #f3f5f7;
      --panel: #ffffff;
      --ink: #17212b;
      --muted: #667085;
      --line: #d6dce5;
      --accent: #155eef;
      --accent-soft: #dbe8ff;
      --danger: #b42318;
      --success: #127a3f;
      --warn: #b35a00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background:
        radial-gradient(circle at top right, #dce8ff 0, transparent 22%),
        linear-gradient(180deg, #eef2f7 0, #f8fafc 100%);
      color: var(--ink);
    }
    .shell { max-width: 1200px; margin: 0 auto; padding: 28px 18px 40px; }
    .hero {
      background: rgba(255, 255, 255, 0.88);
      border: 1px solid rgba(214, 220, 229, 0.9);
      border-radius: 18px;
      padding: 22px 24px;
      backdrop-filter: blur(8px);
      box-shadow: 0 20px 44px rgba(23, 33, 43, 0.08);
    }
    .eyebrow {
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    h1 { margin: 12px 0 6px; font-size: 30px; }
    .meta { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 14px; }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      margin-top: 18px;
    }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
    button {
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, opacity 160ms ease;
    }
    button:hover { transform: translateY(-1px); }
    button.primary { background: var(--accent); color: white; }
    button.secondary { background: #e9eef5; color: var(--ink); }
    button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .card {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
      box-shadow: 0 12px 24px rgba(23, 33, 43, 0.05);
    }
    .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .card .value { margin-top: 8px; font-size: 28px; font-weight: 800; }
    .list { display: grid; gap: 14px; margin-top: 22px; }
    .item {
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 12px 24px rgba(23, 33, 43, 0.06);
    }
    .row-top {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 14px;
      align-items: start;
    }
    .title { font-size: 18px; font-weight: 800; margin: 0; }
    .subtitle { margin-top: 4px; color: var(--muted); font-size: 13px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 800;
      border: 1px solid currentColor;
    }
    .pending { color: var(--warn); }
    .created { color: var(--success); }
    .fields {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .field {
      background: #f7f9fc;
      border: 1px solid #e4e9f1;
      border-radius: 12px;
      padding: 12px;
    }
    .field strong { display: block; margin-bottom: 6px; font-size: 12px; text-transform: uppercase; color: var(--muted); }
    code, pre {
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .status-line { margin-top: 12px; font-size: 13px; color: var(--muted); }
    .hidden { display: none; }
    .empty {
      margin-top: 20px;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 24px;
      color: var(--muted);
      text-align: center;
    }
    a { color: var(--accent); text-decoration: none; }
    @media (max-width: 720px) {
      .row-top { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <span class="eyebrow">Review Required</span>
      <h1 id="title">Loading bug candidates...</h1>
      <div class="meta" id="meta"></div>
      <div class="toolbar">
        <div class="actions">
          <button class="secondary" id="select-all">Select all pending</button>
          <button class="secondary" id="refresh">Refresh</button>
          <button class="primary" id="approve">Create selected bugs</button>
        </div>
        <div class="status-line" id="status-line"></div>
      </div>
      <div class="cards" id="cards"></div>
    </section>
    <section class="list" id="list"></section>
    <section class="empty hidden" id="empty">No pending bug candidates remain in this report.</section>
  </div>
  <script>
    const state = { report: null };

    async function loadReport() {
      const response = await fetch('/api/report');
      state.report = await response.json();
      render();
    }

    function render() {
      const report = state.report;
      const pending = report.candidates.filter(item => !item.created);
      document.getElementById('title').textContent = `${report.execution_key} bug review`;
      document.getElementById('meta').innerHTML = [
        `Started: ${escapeHtml(report.started_at_utc || '-')}`,
        `Finished: ${escapeHtml(report.finished_at_utc || '-')}`,
        `Static report: <a href="file://${escapeAttr(report.report_html_absolute_path)}" target="_blank">${escapeHtml(report.report_html_path)}</a>`,
      ].map(item => `<span>${item}</span>`).join('');
      document.getElementById('cards').innerHTML = [
        card('Total', report.summary.total),
        card('Failed', report.summary.failed),
        card('Unsupported', report.summary.unsupported),
        card('Tool Suggestions', report.summary.tool_suggestions),
        card('MCP Used', report.summary.mcp_healing),
        card('Created', report.summary.bugs),
        card('Candidates', report.summary.bug_candidates),
        card('Pending', report.pending_count),
      ].join('');
      document.getElementById('approve').disabled = pending.length === 0;
      document.getElementById('select-all').disabled = pending.length === 0;

      const list = document.getElementById('list');
      list.innerHTML = report.candidates.map(candidate => item(candidate)).join('');
      document.getElementById('empty').classList.toggle('hidden', pending.length !== 0);
      document.getElementById('status-line').textContent = pending.length
        ? `${pending.length} candidate(s) waiting for manual approval.`
        : 'All bug candidates have been processed.';
    }

    function card(label, value) {
      return `<div class="card"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(String(value))}</div></div>`;
    }

    function item(candidate) {
      const statusClass = candidate.created ? 'created' : 'pending';
      const statusText = candidate.created ? `created${candidate.key ? `: ${candidate.key}` : ''}` : 'pending review';
      const checkedAttr = candidate.created ? 'disabled' : '';
      return `
        <article class="item">
          <div class="row-top">
            <label>
              <input type="checkbox" class="candidate-toggle" value="${escapeAttr(candidate.id)}" ${checkedAttr}>
            </label>
            <div>
              <p class="title">${escapeHtml(candidate.xray_test_key)} · ${escapeHtml(candidate.summary || 'Untitled bug')}</p>
              <div class="subtitle">${escapeHtml(candidate.test_summary || '')}</div>
            </div>
            <span class="pill ${statusClass}">${escapeHtml(statusText)}</span>
          </div>
          <div class="fields">
            ${field('Source', candidate.source || '-')}
            ${field('Failed Step', candidate.failed_step_no ? `${candidate.failed_step_no} - ${candidate.failed_step_text || ''}` : (candidate.failed_step_text || '-'))}
            ${field('Expected', candidate.expected_result || '-')}
            ${field('Actual', candidate.actual_result || '-')}
            ${field('Unsupported', candidate.unsupported || 'no')}
            ${field('Tool Suggestion', candidate.tool_suggestion || 'no')}
            ${field('MCP Used', candidate.mcp_used || 'not started')}
            ${field('Page URL', candidate.page_url || '-')}
            ${field('Screenshot', candidate.screenshot_path || '-')}
            ${field('Error', candidate.error_message || '-')}
          </div>
        </article>
      `;
    }

    function field(label, value) {
      return `<div class="field"><strong>${escapeHtml(label)}</strong><code>${escapeHtml(value)}</code></div>`;
    }

    function selectedIds() {
      return [...document.querySelectorAll('.candidate-toggle:checked')].map(node => node.value);
    }

    async function approveSelected() {
      const ids = selectedIds();
      if (!ids.length) {
        document.getElementById('status-line').textContent = 'Select at least one pending bug candidate.';
        return;
      }
      if (!window.confirm(`Create ${ids.length} selected Jira bug(s)?`)) {
        return;
      }

      const button = document.getElementById('approve');
      button.disabled = true;
      document.getElementById('status-line').textContent = 'Creating Jira bugs...';

      const response = await fetch('/api/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_ids: ids }),
      });
      const payload = await response.json();
      state.report = payload.report;
      const failures = payload.results.filter(item => item.status !== 'created' && item.status !== 'already_created');
      document.getElementById('status-line').textContent = failures.length
        ? `${payload.results.length} processed, ${failures.length} failed.`
        : `${payload.results.length} Jira bug(s) processed successfully.`;
      render();
    }

    document.getElementById('approve').addEventListener('click', approveSelected);
    document.getElementById('refresh').addEventListener('click', loadReport);
    document.getElementById('select-all').addEventListener('click', () => {
      document.querySelectorAll('.candidate-toggle:not(:disabled)').forEach(node => {
        node.checked = true;
      });
    });

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function escapeAttr(value) {
      return escapeHtml(value).replaceAll("'", '&#39;');
    }

    loadReport().catch(error => {
      document.getElementById('title').textContent = 'Bug review could not be loaded';
      document.getElementById('status-line').textContent = error.message;
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
