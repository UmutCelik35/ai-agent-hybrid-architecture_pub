from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

from Bug_Review.bug_review import has_pending_bug_candidates
from Resources.config import Config
from Resources.logger_config import get_logger


logger = get_logger("BugReviewLauncher")


def maybe_launch_review_server(report_json_path: Path) -> None:
    launch_review_server(
        report_json_path,
        require_auto_open=True,
        require_review_mode=True,
        require_pending_candidates=True,
    )


def launch_review_server(
    report_json_path: Path,
    *,
    require_auto_open: bool,
    require_review_mode: bool,
    require_pending_candidates: bool,
) -> bool:
    # Shared launcher used both by automatic post-run flows and manual GUI reopening.
    if require_auto_open and not Config.BUG_REVIEW_UI_AUTO_OPEN:
        logger.info("Bug review UI auto-open is disabled.")
        return False
    if require_review_mode and (Config.BUG_CREATION_MODE or "review").strip().lower() != "review":
        logger.info("Bug review auto-open skipped because BUG_CREATION_MODE is not review.")
        return False
    if not report_json_path.exists():
        logger.warning("Bug review launch skipped because report JSON was not found: %s", report_json_path)
        return False

    try:
        payload = json.loads(report_json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read report JSON for bug review launch: %s", exc)
        return False

    if require_pending_candidates and not has_pending_bug_candidates(payload):
        logger.info("No pending bug candidates were found in %s", report_json_path.name)
        return False

    server_script = Config.PROJECT_ROOT / "Bug_Review" / "review_server.py"
    port = _pick_free_port()
    review_url = f"http://127.0.0.1:{port}/"
    log_path = report_json_path.with_name(f"{report_json_path.stem}_review_server.log")
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            # Start the server as a detached child process and keep its stdout/stderr
            # next to the report for debugging failed launches.
            subprocess.Popen(
                [
                    sys.executable,
                    str(server_script),
                    "--report-json",
                    str(report_json_path),
                    "--port",
                    str(port),
                ],
                cwd=Config.PROJECT_ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if _wait_for_server(review_url):
            opened = webbrowser.open(review_url)
            logger.info(
                "Interactive bug review is ready: %s | browser_opened=%s | log=%s",
                review_url,
                opened,
                log_path.relative_to(Config.PROJECT_ROOT).as_posix(),
            )
            return True
        else:
            logger.warning(
                "Interactive bug review server did not become ready. URL=%s | log=%s",
                review_url,
                log_path.relative_to(Config.PROJECT_ROOT).as_posix(),
            )
    except Exception as exc:
        logger.warning("Could not launch interactive bug review: %s", exc)
    return False


def _pick_free_port() -> int:
    # Port 0 asks the OS for an available ephemeral port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(review_url: str, attempts: int = 30, delay_seconds: float = 0.2) -> bool:
    # Poll the JSON endpoint instead of the browser page so readiness means the
    # server can already read and serve the report payload.
    api_url = f"{review_url}api/report"
    for _ in range(attempts):
        try:
            with urlopen(api_url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(delay_seconds)
    return False
