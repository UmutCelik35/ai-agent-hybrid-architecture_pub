import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

# Find the project root directory based on the location of this file.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure the project root is in the system path so we can import internal modules.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Jira_Aut.xray_client import XrayClient
from Bug_Review.review_launcher import maybe_launch_review_server
from Resources.config import Config
from Resources.logger_config import get_logger
from Static_Aut.reporting.html_reporter import write_execution_html_report

logger = get_logger("XrayAuto")

# Define the path to the individual test runner script.
PLAYWRIGHT_XRAY_SCRIPT = PROJECT_ROOT / "Web_Aut" / "playwright_xray.py"


def parse_args():
    """
    Parses command-line arguments.
    Allows passing the execution key and a fail-fast flag via terminal.
    """
    parser = argparse.ArgumentParser(
        description="Run Playwright automation from an Xray Test Execution issue"
    )
    parser.add_argument("--execution-key", help="Xray Test Execution issue key, for example PROJ-244")
    parser.add_argument(
        "--execution_mode",
        default="web",
        help="Execution mode to run. Currently only 'web' is supported.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failed test execution.",
    )
    parser.add_argument(
        "--xray-deployment",
        choices=["datacenter", "cloud"],
        default="",
        help="Override XRAY_DEPLOYMENT for this run.",
    )
    return parser.parse_args()


def _extract_test_keys(payload):
    """Safely extract test issue keys from the Xray API payload."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("tests") or payload.get("issues") or payload.get("data") or []
    else:
        items = []

    test_keys = []
    for item in items:
        if isinstance(item, str):
            key = item.strip()
        elif isinstance(item, dict):
            key = (
                item.get("key")
                or item.get("issueKey")
                or item.get("testKey")
                or item.get("issue", {}).get("key")
                or ""
            )
            key = str(key).strip()
        else:
            key = ""

        if key:
            logger.debug(f"Extracted test key: {key}")
            test_keys.append(key)

    logger.debug(f"Extracted test keys before deduplication: {test_keys}")
    return list(dict.fromkeys(test_keys))


def main() -> int:
    args = parse_args()
    if args.xray_deployment:
        os.environ["XRAY_DEPLOYMENT"] = args.xray_deployment
        Config.XRAY_DEPLOYMENT = args.xray_deployment
    started_at_utc = datetime.now(timezone.utc).isoformat()
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_dir = PROJECT_ROOT / "logs" / "execution_reports" / "raw" / f"{args.execution_key}_{run_stamp}"
    test_results = []

    try:
        execution_mode = (args.execution_mode or "").strip().lower()
        if execution_mode != "web":
            logger.error("Unsupported execution mode '%s'. Only 'web' is supported.", args.execution_mode)
            return 1

        Config.validate()
        jira_url = Config.JIRA_CLOUD_URL if Config.XRAY_DEPLOYMENT == "cloud" else Config.JIRA_DATACENTER_URL
        logger.info(
            "Step 1: Reading Xray Test Execution %s from %s (%s)...",
            args.execution_key,
            jira_url,
            Config.XRAY_DEPLOYMENT,
        )

        xray_client = XrayClient()
        tests_payload = xray_client.get_test_execution_tests(args.execution_key)
        logger.debug(f"Tests payload: {tests_payload}")

        test_keys = _extract_test_keys(tests_payload)
        logger.debug(f"Extracted test keys: {test_keys}")

        if not test_keys:
            logger.error(f"No runnable tests were found in Xray Test Execution {args.execution_key}")
            return 1

        logger.info(f"Step 2: Found {len(test_keys)} test(s) in execution {args.execution_key}.")

        failed_tests = []
        result_dir.mkdir(parents=True, exist_ok=True)

        for index, test_key in enumerate(test_keys, start=1):
            logger.info(f"Step 3.{index}: Running test {test_key}...")
            result_json = result_dir / f"{test_key}.json"

            result = subprocess.run(
                [
                    sys.executable,
                    str(PLAYWRIGHT_XRAY_SCRIPT),
                    "--test-key",
                    test_key,
                    "--execution-key",
                    args.execution_key,
                    "--result-json",
                    str(result_json),
                    "--xray-deployment",
                    Config.XRAY_DEPLOYMENT,
                ],
                cwd=PROJECT_ROOT,
            )
            test_result = _load_test_result(result_json, test_key, result.returncode)
            test_results.append(test_result)

            run_failures = []
            if result.returncode != 0:
                run_failures.append(test_key)

            if run_failures:
                failed_tests.extend(run_failures)
                if args.fail_fast:
                    logger.warning("Fail-fast is enabled. Stopping after the first failed test.")
                    break
            else:
                logger.info(f"Test {test_key} completed.")

        report_path = write_execution_html_report(
            args.execution_key,
            test_results,
            started_at_utc,
            datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "Execution HTML report written to %s",
            report_path.relative_to(PROJECT_ROOT).as_posix(),
        )
        maybe_launch_review_server(report_path.with_suffix(".json"))

        if failed_tests:
            logger.error(f"Completed with failures. Failed tests: {', '.join(failed_tests)}")
            return 1

        logger.info("All tests in the Test Execution completed successfully.")
        return 0

    except KeyboardInterrupt:
        logger.warning("Execution interrupted.")
        return 1
    except Exception as exc:
        logger.exception(f"A critical error occurred during execution: {exc}")
        return 1


def _load_test_result(result_json: Path, test_key: str, return_code: int) -> dict:
    if result_json.exists():
        try:
            payload = json.loads(result_json.read_text(encoding="utf-8"))
            payload.setdefault("test_key", test_key)
            payload.setdefault("return_code", return_code)
            return payload
        except Exception as exc:
            logger.warning("Could not read test result JSON for %s: %s", test_key, exc)

    return {
        "test_key": test_key,
        "final_status": "unknown",
        "return_code": return_code,
        "error_message": "The test runner did not write a structured result JSON.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
