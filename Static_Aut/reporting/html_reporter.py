from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Resources.config import Config


REPORT_DIR = Config.PROJECT_ROOT / "logs" / "execution_reports"


def write_execution_html_report(
    execution_key: str,
    test_results: list[dict[str, Any]],
    started_at_utc: str,
    finished_at_utc: str,
    output_dir: Path | None = None,
) -> Path:
    output_dir = output_dir or REPORT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_key = _safe_name(execution_key or "execution")
    html_path = output_dir / f"{safe_key}_{stamp}.html"
    json_path = html_path.with_suffix(".json")

    payload = {
        "execution_key": execution_key,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": _summary(test_results),
        "test_results": test_results,
        "report_html_path": _relative(html_path),
        "report_json_path": _relative(json_path),
    }
    write_report_files(payload, html_path, json_path)
    return html_path


def write_report_files(payload: dict[str, Any], html_path: Path, json_path: Path) -> None:
    payload["summary"] = _summary(payload.get("test_results") or [])
    payload["report_html_path"] = _relative(html_path)
    payload["report_json_path"] = _relative(json_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_render(payload, json_path), encoding="utf-8")


def _summary(test_results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(test_results),
        "passed": sum(1 for result in test_results if _is_passed(result)),
        "failed": sum(1 for result in test_results if _is_failed(result)),
        "unsupported": sum(1 for result in test_results if _is_unsupported(result)),
        "tool_suggestions": sum(1 for result in test_results if result.get("tool_suggestion", {}).get("created")),
        "mcp_healing": sum(
            1
            for result in test_results
            if result.get("static", {}).get("healing_attempted") or result.get("mcp_full_scenario_used")
        ),
        "bugs": sum(1 for result in test_results if result.get("bug", {}).get("created")),
        "bug_candidates": sum(1 for result in test_results if result.get("bug", {}).get("candidate")),
    }


def _render(payload: dict[str, Any], json_path: Path) -> str:
    rows = "\n".join(_render_row(result) for result in payload["test_results"])
    cards = "\n".join(
        _card(label, value)
        for label, value in (
            ("Total", payload["summary"]["total"]),
            ("Passed", payload["summary"]["passed"]),
            ("Failed", payload["summary"]["failed"]),
                ("Unsupported", payload["summary"]["unsupported"]),
                ("MCP Used", payload["summary"]["mcp_healing"]),
                ("Bugs", payload["summary"]["bugs"]),
                ("Candidates", payload["summary"]["bug_candidates"]),
            )
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(payload["execution_key"])} Execution Report</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #667085;
      --border: #d9dee7;
      --pass: #147a3f;
      --fail: #b42318;
      --warn: #b35a00;
      --info: #175cd3;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 28px 32px 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    .meta {{ color: var(--muted); font-size: 13px; display: flex; gap: 18px; flex-wrap: wrap; }}
    main {{ padding: 24px 32px 36px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .card .value {{ margin-top: 6px; font-size: 24px; font-weight: 650; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{ color: var(--muted); background: #fbfcfe; font-weight: 600; }}
    tr:last-child td {{ border-bottom: 0; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 650;
      border: 1px solid currentColor;
      white-space: nowrap;
    }}
    .pass {{ color: var(--pass); }}
    .fail {{ color: var(--fail); }}
    .warn {{ color: var(--warn); }}
    .info {{ color: var(--info); }}
    .muted {{ color: var(--muted); }}
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; color: var(--info); }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f2f4f7;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      max-height: 360px;
      overflow: auto;
      font-size: 12px;
    }}
    .small {{ font-size: 12px; }}
    @media (max-width: 860px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--border); }}
      td {{ border-bottom: 0; padding: 8px 12px; }}
      td::before {{ content: attr(data-label); display: block; color: var(--muted); font-size: 11px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{_e(payload["execution_key"])} Web Execution Report</h1>
    <div class="meta">
      <span>Started: {_e(payload["started_at_utc"])}</span>
      <span>Finished: {_e(payload["finished_at_utc"])}</span>
      <span>JSON: {_e(_relative(json_path))}</span>
    </div>
  </header>
  <main>
    <section class="cards">{cards}</section>
    <table>
      <thead>
        <tr>
          <th>Test</th>
          <th>Result</th>
          <th>Unsupported</th>
          <th>Suggestion</th>
          <th>MCP</th>
          <th>Bug</th>
          <th>Details</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
</body>
</html>
"""


def _render_row(result: dict[str, Any]) -> str:
    status = str(result.get("final_status") or "unknown")
    static = result.get("static") or {}
    suggestion = result.get("tool_suggestion") or {}
    bug = result.get("bug") or {}
    unsupported_text = _unsupported_text(result)
    detail_payload = {
        "summary": result.get("test_summary"),
        "return_code": result.get("return_code"),
        "duration_seconds": result.get("duration_seconds"),
        "static": static,
        "llm_action_routing": result.get("llm_action_routing"),
        "tool_suggestion": suggestion,
        "bug": bug,
        "error_message": result.get("error_message"),
    }
    return f"""<tr>
  <td data-label="Test"><strong>{_e(result.get("test_key"))}</strong><br><span class="muted small">{_e(result.get("test_summary"))}</span></td>
  <td data-label="Result">{_badge(status, _status_class(result))}<br><span class="muted small">{_e(static.get("summary"))}</span></td>
  <td data-label="Unsupported">{unsupported_text}</td>
  <td data-label="Suggestion">{_yes_no(suggestion.get("created"), "info")}</td>
  <td data-label="MCP">{_mcp_text(result, static)}</td>
  <td data-label="Bug">{_bug_text(bug)}</td>
  <td data-label="Details"><details><summary>Open</summary><pre>{_e(json.dumps(detail_payload, ensure_ascii=False, indent=2))}</pre></details></td>
</tr>"""


def _card(label: str, value: int) -> str:
    return f'<div class="card"><div class="label">{_e(label)}</div><div class="value">{value}</div></div>'


def _badge(text: str, css_class: str) -> str:
    return f'<span class="badge {css_class}">{_e(text)}</span>'


def _yes_no(value: Any, yes_class: str = "pass") -> str:
    return _badge("yes", yes_class) if value else '<span class="muted">no</span>'


def _mcp_text(result: dict[str, Any], static: dict[str, Any]) -> str:
    if result.get("mcp_full_scenario_used"):
        return _badge("full scenario", "info")
    if not static.get("healing_attempted"):
        return '<span class="muted">not started</span>'
    patch = static.get("healing_patch") or {}
    if patch.get("selector"):
        return f'{_badge("started", "info")}<br><span class="small">{_e(patch["selector"])}</span>'
    if patch.get("error"):
        return f'{_badge("failed", "warn")}<br><span class="small">{_e(patch["error"])}</span>'
    return f'{_badge("started", "info")}<br><span class="small">no locator patch</span>'


def _bug_text(bug: dict[str, Any]) -> str:
    if bug.get("created"):
        return _badge(str(bug.get("key") or "created"), "fail")
    if bug.get("candidate"):
        return _badge("pending review", "warn")
    if bug.get("status") == "disabled":
        return '<span class="muted">disabled</span>'
    if bug.get("status") == "skipped_unconfigured":
        return '<span class="muted">not configured</span>'
    if not bug.get("created"):
        return '<span class="muted">no</span>'
    return _badge(str(bug.get("key") or "created"), "fail")


def _unsupported_text(result: dict[str, Any]) -> str:
    if result.get("non_executable_expected_result"):
        return _badge("expected result", "warn")
    steps = result.get("static", {}).get("unsupported_steps") or []
    if steps:
        return f'{_badge("step", "warn")}<br><span class="small">{_e("; ".join(step.get("text", "") for step in steps))}</span>'
    return '<span class="muted">no</span>'


def _status_class(result: dict[str, Any]) -> str:
    if _is_passed(result):
        return "pass"
    if _is_unsupported(result):
        return "warn"
    if _is_failed(result):
        return "fail"
    return "info"


def _is_passed(result: dict[str, Any]) -> bool:
    return str(result.get("final_status", "")).startswith("passed")


def _is_failed(result: dict[str, Any]) -> bool:
    status = str(result.get("final_status", ""))
    return bool(result.get("return_code")) and not _is_unsupported(result) and not status.startswith("passed")


def _is_unsupported(result: dict[str, Any]) -> bool:
    status = str(result.get("final_status", ""))
    return "unsupported" in status


def _safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return value.strip("_") or "execution"


def _relative(path: Path) -> str:
    try:
        return path.relative_to(Config.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))
