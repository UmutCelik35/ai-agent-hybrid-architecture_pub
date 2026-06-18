from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from Jira_Aut.jira_bug_client import FailureReport, JiraBugClient
from Resources.config import Config
from Resources.logger_config import get_logger


logger = get_logger("BugReview")


def empty_bug_result() -> dict[str, Any]:
    # Shared default shape for cases where no Jira bug or review candidate is needed.
    return {
        "created": False,
        "key": "",
        "candidate": False,
        "approved": False,
        "status": "not_requested",
    }


def build_bug_candidate(
    report: FailureReport,
    project_key: str,
    source: str,
    test_summary: str = "",
) -> dict[str, Any]:
    # Store the full failure report inside the candidate so the review UI can
    # create the Jira issue later without re-running the failed test.
    candidate_id = _candidate_id(report, project_key, source)
    screenshot_path = _relative_path(report.screenshot_path)
    return {
        "created": False,
        "key": "",
        "candidate": True,
        "approved": False,
        "status": "pending_review",
        "id": candidate_id,
        "source": source,
        "project_key": project_key,
        "xray_test_key": report.xray_test_key,
        "summary": report.summary,
        "test_summary": test_summary,
        "failed_step_no": report.failed_step_no,
        "failed_step_text": report.failed_step_text,
        "expected_result": report.expected_result,
        "actual_result": report.actual_result,
        "page_url": report.page_url,
        "screenshot_path": screenshot_path,
        "error_message": report.error_message,
        "candidate_payload": {
            "project_key": project_key,
            "failure_report": asdict(report),
            "source": source,
            "test_summary": test_summary,
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def resolve_bug_action(
    report: FailureReport,
    project_key: str,
    source: str,
    test_summary: str = "",
) -> dict[str, Any]:
    # BUG_CREATION_MODE controls whether failures become review candidates,
    # immediate Jira issues, or no bug records at all.
    if not project_key:
        bug = empty_bug_result()
        bug["status"] = "skipped_unconfigured"
        bug["error"] = "BUG_PROJECT_KEY is not configured."
        return bug

    mode = (Config.BUG_CREATION_MODE or "review").strip().lower()
    if mode == "off":
        bug = empty_bug_result()
        bug["status"] = "disabled"
        return bug

    candidate = build_bug_candidate(report, project_key, source, test_summary=test_summary)
    if mode == "review":
        return candidate

    if mode == "auto":
        return create_bug_from_candidate(candidate)

    logger.warning("Unknown BUG_CREATION_MODE '%s'; defaulting to review.", mode)
    return candidate


def create_bug_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    # Review approval and auto mode both arrive here with the same candidate payload.
    if candidate.get("created"):
        return candidate

    payload = candidate.get("candidate_payload") or {}
    failure_report_payload = payload.get("failure_report") or {}
    project_key = str(payload.get("project_key") or candidate.get("project_key") or "").strip()
    if not project_key:
        candidate = dict(candidate)
        candidate["status"] = "skipped_unconfigured"
        candidate["error"] = "BUG_PROJECT_KEY is not configured."
        return candidate

    report = FailureReport.from_dict(failure_report_payload)
    try:
        # The Jira issue key is written back into the candidate so reports can
        # show the final created issue instead of the pending-review state.
        issue_key = JiraBugClient().create_bug(report, project_key)
    except Exception as exc:
        candidate = dict(candidate)
        candidate["status"] = "create_failed"
        candidate["error"] = str(exc)
        logger.exception("Bug creation failed for candidate %s: %s", candidate.get("id"), exc)
        return candidate

    candidate = dict(candidate)
    candidate.update(
        {
            "created": True,
            "key": issue_key,
            "approved": True,
            "status": "created",
            "opened_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": "",
        }
    )
    return candidate


def has_pending_bug_candidates(report_payload: dict[str, Any]) -> bool:
    # Used by the launcher to avoid opening the review UI when there is nothing to approve.
    return any(_is_pending_candidate(result.get("bug") or {}) for result in report_payload.get("test_results") or [])


def iter_pending_bug_candidates(report_payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for result in report_payload.get("test_results") or []:
        bug = result.get("bug") or {}
        if _is_pending_candidate(bug):
            candidates.append(bug)
    return candidates


def _is_pending_candidate(bug: dict[str, Any]) -> bool:
    return bool(bug.get("candidate")) and not bug.get("created")


def _candidate_id(report: FailureReport, project_key: str, source: str) -> str:
    # Build a deterministic short ID so the same failure details produce the
    # same review candidate identity across report reads.
    digest = hashlib.sha256(
        "|".join(
            [
                source,
                project_key,
                report.xray_test_key,
                str(report.failed_step_no or ""),
                report.summary,
                report.failed_step_text,
                report.actual_result,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _relative_path(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        return path.relative_to(Config.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)
