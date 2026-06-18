import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Resources.logger_config import get_logger

logger = get_logger()

XRAY_SCRIPT = PROJECT_ROOT / "Web_Aut" / "playwright_xray_execution.py"


def parse_args():
    parser = argparse.ArgumentParser(description="Run Playwright Xray automation")
    parser.add_argument(
        "--mode",
        default="web",
        help="Execution mode to run. Currently only 'web' is supported.",
    )
    parser.add_argument("--execution-key", help="Xray Test Execution issue key")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failed test execution.",
    )
    return parser.parse_args()


def prompt_value(label: str, warning_message: str) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        logger.warning(warning_message)


def main() -> int:
    args = parse_args()
    logger.info("Xray Automation Runner")

    execution_mode = (args.mode or "").strip().lower()
    if execution_mode != "web":
        logger.error("Unsupported execution mode '%s'. Only 'web' is supported.", args.mode)
        return 1

    execution_key = args.execution_key or prompt_value(
        "Test Execution key: ",
        "Please input correct test execution key",
    )

    command = [
        sys.executable,
        str(XRAY_SCRIPT),
        "--execution-key",
        execution_key,
        "--execution_mode",
        execution_mode,
    ]
    logger.info(f"Running {execution_mode.upper()} Test Execution '{execution_key}'...")

    if args.fail_fast:
        command.append("--fail-fast")

    logger.debug(f"Run with command: {' '.join(command)}")
    
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
