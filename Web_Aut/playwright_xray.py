import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Bug_Review.bug_review import empty_bug_result, resolve_bug_action
from Bug_Review.review_launcher import maybe_launch_review_server
from Jira_Aut.jira_bug_client import FailureReport, JiraBugClient
from Jira_Aut.xray_client import XrayClient
from Jira_Aut.xray_normalizer import format_xray_scenario
from Resources.config import Config
from Resources.logger_config import get_logger
from Static_Aut.healing.healing_runner import (
    run_static_locator_healing,
    should_run_static_locator_healing,
)
from Static_Aut.toolbox.static_toolbox import ScenarioStep, StaticExecutionStatus, analyze_static_web_tool_coverage
from Static_Aut.routing.tool_router import route_unsupported_steps_with_llm
from Static_Aut.routing.tool_suggester import suggest_static_tools
from Static_Aut.execution.web_executors import execute_static_web_flow
from Static_Aut.reporting.html_reporter import write_execution_html_report
from Web_Aut.mcp_full_scenario_executor import execute_with_mcp_full_scenario


logger = get_logger("PlaywrightAgent")


def parse_args():
    parser = argparse.ArgumentParser(description="Run Playwright automation from an Xray Test issue")
    parser.add_argument("--test-key", help="Xray Test issue key, for example PROJ-237")
    parser.add_argument("--execution-key", default="", help="Optional Xray Test Execution issue key.")
    parser.add_argument("--result-json", default="", help="Optional path for structured per-test result JSON.")
    parser.add_argument(
        "--xray-deployment",
        choices=["datacenter", "cloud"],
        default="",
        help="Override XRAY_DEPLOYMENT for this run.",
    )
    # --result-json can be used to write the test result payload to a file. This file will contain information about the test execution, such as the final status, duration, any errors encountered, and information about static execution and tool suggestions if applicable.
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if args.xray_deployment:
        os.environ["XRAY_DEPLOYMENT"] = args.xray_deployment
        Config.XRAY_DEPLOYMENT = args.xray_deployment
    started_at = datetime.now(timezone.utc)
    result_payload = _new_test_result(args.test_key, args.execution_key, started_at) # This initializes the result payload with the test key, execution key, and start time. The payload will be updated throughout the execution with the test summary, final status, return code, and other relevant information before being written to the result JSON file at the end of the test.
    action_suggestion_path = None # This will hold the path to any generated static tool suggestion file, which can be included in the final test result payload for reporting and debugging purposes.
    action_routing_payload = {"attempted": False, "routes": []} # This will track whether LLM fallback routing was attempted for unsupported static steps and what routes were suggested by the LLM, which can be included in the final test result payload for reporting and debugging purposes.

    try:
        Config.validate() # Ensure all necessary configuration for Xray reading and LLM usage is present before starting execution.
        jira_url = Config.JIRA_CLOUD_URL if Config.XRAY_DEPLOYMENT == "cloud" else Config.JIRA_DATACENTER_URL
        logger.info(
            "Step 1: Reading Xray Test %s from %s (%s)...",
            args.test_key,
            jira_url,
            Config.XRAY_DEPLOYMENT,
        )
 
        xray_client = XrayClient()
        test_summary_payload = xray_client.get_test_summary(args.test_key) # This should include the test summary and any relevant metadata needed for scenario formatting.
        steps_payload = xray_client.get_test_steps(args.test_key) # This should include the list of test steps with their descriptions and any associated data that will be used for scenario formatting and static execution.
        result_payload["test_summary"] = _extract_test_summary(test_summary_payload) # This extracts a clean test summary and adds it to the result payload for reporting purposes. The test summary is also used in the scenario text that is sent to the static executor and MCP, so it's important to have it properly extracted and formatted.

        logger.debug(f"Test summary payload: {test_summary_payload}") # such as {'fields': {'summary': 'Verify that the user can log in successfully'}} or an error message if the issue key is invalid or the summary field is missing.
        logger.debug(f"Test steps payload: {steps_payload}") # such as 
        scenario_text = format_xray_scenario(args.test_key, test_summary_payload, steps_payload)
        logger.debug(f"Formatted scenario text to send static executor:\n{scenario_text}")

        static_mode = (Config.STATIC_TOOLBOX_MODE or "on").strip().lower()
        if static_mode == "off":
            logger.info("Static toolbox mode is off. Running the full scenario with MCP only.")
            exit_code, bug = await execute_with_mcp_full_scenario(
                args.test_key,
                scenario_text,
                test_summary=result_payload["test_summary"],
            )
            return _finalize_test_result(
                args,
                result_payload,
                "passed_mcp_full" if exit_code == 0 else "failed_mcp_full",
                exit_code,
                mcp_full_scenario_used=True,
                bug=bug,
            )
        if static_mode == "on":
            static_result = await execute_static_web_flow(args.test_key, scenario_text)
        else:
            static_result = analyze_static_web_tool_coverage(
                scenario_text,
                mode=Config.STATIC_TOOLBOX_MODE,
            )

        _log_static_result(static_result)

        if static_mode == "shadow":
            logger.info("Static toolbox shadow analysis completed. No browser execution was started.")
            exit_code = 0 if static_result.status != StaticExecutionStatus.UNSUPPORTED else 1
            return _finalize_test_result(
                args,
                result_payload,
                "passed_shadow" if exit_code == 0 else "unsupported_shadow",
                exit_code,
                static_result=static_result,
            )

        if (
            static_mode == "on"
            and static_result.status == StaticExecutionStatus.UNSUPPORTED
            and static_result.unsupported_steps 
            # static_result.unsupported_steps = [
            #     ScenarioStep(number=2, text='Upload file "resume.pdf"'),
            #     ScenarioStep(number=5, text='Drag "Card A" to "Done" column'),
            # ]
            and Config.STATIC_LLM_ROUTER_ENABLED
        ):
            try:
                logger.info("Step 1.3: Requesting LLM fallback routing for unsupported static steps...")
                action_routing_payload["attempted"] = True
                routing_result = await route_unsupported_steps_with_llm(
                    scenario_text,
                    static_result.unsupported_steps,
                )
                if routing_result.routes:
                    action_routing_payload["routes"] = [
                        {
                            "step_no": route.step_no,
                            "tool_name": route.tool_name,
                            "confidence": route.confidence,
                            "reason": route.reason,
                        }
                        for route in routing_result.routes
                    ]
                    logger.info(
                        "LLM static routing token usage | prompt_tokens=%s | completion_tokens=%s | total_tokens=%s",
                        routing_result.prompt_tokens,
                        routing_result.completion_tokens,
                        routing_result.total_tokens,
                    )
                    for route in routing_result.routes:
                        logger.info(
                            "LLM static route accepted | step=%s | tool=%s | confidence=%.2f | reason=%s",
                            route.step_no,
                            route.tool_name,
                            route.confidence,
                            route.reason,
                        )
                    static_result = await execute_static_web_flow( # execute_static_web_flow runs with routded tools by llm this time
                        args.test_key,
                        scenario_text,
                        tool_overrides=routing_result.tool_overrides,
                    )
                    logger.info(
                        "Static toolbox retry after LLM routing | status=%s | summary=%s",
                        static_result.status.value,
                        static_result.summary,
                    )
                    _log_static_result(static_result)
                else:
                    logger.info("LLM fallback routing did not map any unsupported steps.")
            except Exception as exc:
                logger.exception(f"LLM fallback routing failed: {exc}")

        if (
            static_result.status == StaticExecutionStatus.UNSUPPORTED
            and static_result.unsupported_steps
            and Config.STATIC_TOOL_SUGGESTIONS_ENABLED
        ):
            
            try:
                logger.info("Step 1.4: Requesting static tool suggestions for unsupported steps...")
                suggestion_path = await suggest_static_tools(
                    args.test_key,
                    scenario_text,
                    static_result.unsupported_steps,
                )
                action_suggestion_path = suggestion_path
                if suggestion_path:
                    logger.info(
                        "Static tool suggestions were written to %s",
                        suggestion_path.relative_to(Config.PROJECT_ROOT).as_posix(),
                    )
            except Exception as exc:
                logger.exception(f"Static tool suggestion generation failed: {exc}")

        if should_run_static_locator_healing(
            static_result,
            static_mode,
            Config.STATIC_SELF_HEALING_ENABLED,
        ):
            static_result = await run_static_locator_healing(
                args.test_key,
                scenario_text,
                static_result,
                logger,
            )

        if _is_non_executable_expected_result(static_result):
            expected_suggestion_path = await _handle_non_executable_expected_result(
                args.test_key,
                scenario_text,
                static_result,
            )
            return _finalize_test_result(
                args,
                result_payload,
                "unsupported_expected_result",
                1,
                static_result=static_result,
                tool_suggestion_path=expected_suggestion_path or action_suggestion_path,
                action_routing=action_routing_payload,
                non_executable_expected_result=True,
            )

        if static_result.status == StaticExecutionStatus.PASSED:
            logger.info("Static toolbox execution passed. MCP full scenario execution is skipped.")
            logger.debug(f"Final static automation report text:\n{static_result.report_text}")
            try:
                report = JiraBugClient.parse_automation_report(static_result.report_text)
                logger.debug(f"Parsed static report details: {report}")
            except Exception as exc:
                logger.exception(f"Could not parse static automation report: {exc}")
                return _finalize_test_result(
                    args,
                    result_payload,
                    "critical_error",
                    1,
                    static_result=static_result,
                    tool_suggestion_path=action_suggestion_path,
                    action_routing=action_routing_payload,
                    error_message=str(exc),
                )

            logger.info("Step 3: Scenario passed via static toolbox. No bug issue was created.")
            return _finalize_test_result(
                args,
                result_payload,
                "passed_static",
                0,
                static_result=static_result,
                tool_suggestion_path=action_suggestion_path,
                action_routing=action_routing_payload,
            )

        if static_result.status == StaticExecutionStatus.FAILED and static_result.failed_locator_key:
            if getattr(static_result, "failure_from_expected_validation", False):
                logger.error(
                    "Static run failed on Expected Result assertion; skipping MCP locator healing and preparing a bug review candidate."
                )
            else:
                logger.error(
                    "Static locator failure was not resolved by MCP locator discovery. "
                    "MCP full scenario execution is not allowed for locator failures."
                )
            bug = _create_bug_for_unresolved_static_failure(
                args.test_key,
                static_result,
                test_summary=result_payload["test_summary"],
            )
            return _finalize_test_result(
                args,
                result_payload,
                "failed_expected_assertion"
                if getattr(static_result, "failure_from_expected_validation", False)
                else "failed_static_locator",
                1,
                static_result=static_result,
                tool_suggestion_path=action_suggestion_path,
                action_routing=action_routing_payload,
                bug=bug,
            )

        logger.error(
            "Static toolbox did not pass and no locator-specific MCP repair path is available. "
            "MCP is not used for full scenario execution."
        )
        return _finalize_test_result(
            args,
            result_payload,
            "unsupported_step"
            if static_result.status == StaticExecutionStatus.UNSUPPORTED
            else "failed_static",
            1,
            static_result=static_result,
            tool_suggestion_path=action_suggestion_path,
            action_routing=action_routing_payload,
        )

    except Exception as exc:
        logger.exception(f"A critical error occurred during the execution of {args.test_key}: {exc}")
        return _finalize_test_result(
            args,
            result_payload,
            "critical_error",
            1,
            error_message=str(exc),
            action_routing=action_routing_payload,
        )


def _new_test_result(test_key: str, execution_key: str, started_at: datetime) -> dict:
    return {
        "test_key": test_key,
        "execution_key": execution_key,
        "started_at_utc": started_at.isoformat(), # such as "2024-11-14T12:34:56.123456+00:00", useful for human-readable reporting and debugging
        "_started_at_epoch": started_at.timestamp(), # such as 1700000000.123, used for duration calculation at the end of the test. It is removed from the final payload before writing the result to avoid confusion, since it's an internal field.
        "test_summary": "",
        "final_status": "running",
        "return_code": None,
    }


def _finalize_test_result(
    args,
    payload: dict,
    final_status: str,
    return_code: int,
    static_result=None,
    tool_suggestion_path=None,
    action_routing: dict | None = None,
    bug: dict | None = None,
    error_message: str = "",
    non_executable_expected_result: bool = False,
    mcp_full_scenario_used: bool = False,
) -> int:
    finished_at = datetime.now(timezone.utc)
    started_at_epoch = payload.pop("_started_at_epoch", finished_at.timestamp())
    payload.update(
        {
            "final_status": final_status,
            "return_code": return_code,
            "finished_at_utc": finished_at.isoformat(),
            "duration_seconds": round(finished_at.timestamp() - started_at_epoch, 3),
            "non_executable_expected_result": non_executable_expected_result,
            "mcp_full_scenario_used": mcp_full_scenario_used,
            "error_message": error_message,
            "llm_action_routing": action_routing or {"attempted": False, "routes": []},
            "tool_suggestion": _tool_suggestion_payload(tool_suggestion_path),
            "bug": bug or empty_bug_result(),
        }
    )
    if static_result is not None:
        payload["static"] = _static_result_payload(static_result)
    _write_test_result(args.result_json, payload)
    _write_single_test_html_report(args, payload)
    return return_code


def _write_test_result(result_json: str, payload: dict) -> None:
    if not result_json:
        return
    output_path = Path(result_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload["result_json_path"] = _relative_path(output_path)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_single_test_html_report(args, payload: dict) -> None:
    if args.result_json or args.execution_key:
        return
    report_path = write_execution_html_report(
        args.test_key,
        [payload],
        payload.get("started_at_utc", ""),
        payload.get("finished_at_utc", ""),
    )
    logger.info(
        "Single test HTML report written to %s",
        report_path.relative_to(Config.PROJECT_ROOT).as_posix(),
    )
    maybe_launch_review_server(report_path.with_suffix(".json"))


def _static_result_payload(static_result) -> dict:
    return {
        "status": getattr(static_result.status, "value", str(static_result.status)),
        "summary": static_result.summary,
        "app_name": static_result.app_name,
        "matched_tools": static_result.matched_tools,
        "unsupported_steps": [
            {"number": step.number, "text": step.text}
            for step in static_result.unsupported_steps
        ],
        "error_message": static_result.error_message,
        "failed_tool": static_result.failed_tool,
        "failed_locator_key": static_result.failed_locator_key,
        "tried_selectors": static_result.tried_selectors,
        "page_url": static_result.page_url,
        "healing_attempted": static_result.healing_attempted,
        "healing_patch": static_result.healing_patch,
        "tool_overrides": static_result.tool_overrides,
        "failure_from_expected_validation": getattr(static_result, "failure_from_expected_validation", False),
    }


def _tool_suggestion_payload(suggestion_path) -> dict:
    if not suggestion_path:
        return {"created": False}
    path = Path(suggestion_path)
    payload = {"created": True, "path": _relative_path(path)}
    if path.exists():
        try:
            payload["content"] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            payload["read_error"] = str(exc)
    return payload


def _extract_test_summary(test_summary_payload) -> str:
    if not isinstance(test_summary_payload, dict):
        return ""
    fields = test_summary_payload.get("fields") or {}
    summary = fields.get("summary") or test_summary_payload.get("summary") or ""
    return str(summary).strip()


def _relative_path(path: Path) -> str:
    try:
        return path.relative_to(Config.PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _log_static_result(static_result) -> None:
    if static_result.status == StaticExecutionStatus.DISABLED:
        logger.error(
            "Static execution disabled due to configuration. No static analysis was performed on this scenario. Please check STATIC_TOOLBOX_MODE and related settings if you expected static execution to run."
        )
        return

    logger.info(
        "Static toolbox result | status=%s | app=%s | summary=%s | matched_tools=%s",
        static_result.status.value,
        static_result.app_name or "unknown",
        static_result.summary,
        ", ".join(static_result.matched_tools) or "none",
    )
    if static_result.unsupported_steps:
        logger.info(
            "Static toolbox unsupported steps: %s",
            "; ".join(
                f"{step.number}. {step.text}"
                for step in static_result.unsupported_steps
            ),
        )
    if static_result.status == StaticExecutionStatus.FAILED:
        logger.warning(
            "Static toolbox error before self-healing decision: %s",
            static_result.error_message or "No error detail provided.",
        )
        if static_result.failed_locator_key:
            logger.info(
                "Static locator failure details | tool=%s | locator_key=%s | tried_selectors=%s",
                static_result.failed_tool or "unknown",
                static_result.failed_locator_key,
                ", ".join(static_result.tried_selectors) or "none",
            )


def _is_non_executable_expected_result(static_result) -> bool:
    return (
        static_result.status == StaticExecutionStatus.FAILED
        and static_result.failed_tool == "expected_result_validation"
    )


async def _handle_non_executable_expected_result(test_key: str, scenario_text: str, static_result) -> Path | None:
    logger.error(
        "Static run stopped because an Expected Result is not executable. "
        "No product bug will be created for this test-design/automation coverage issue."
    )
    unsupported_step = _expected_result_as_unsupported_step(static_result)
    if not Config.STATIC_TOOL_SUGGESTIONS_ENABLED:
        logger.info("Static tool suggestion generation is disabled; no suggestion file was written.")
        return None
    try:
        logger.info("Requesting static tool suggestions for non-executable Expected Result...")
        suggestion_path = await suggest_static_tools(
            test_key,
            scenario_text,
            [unsupported_step],
        )
        if suggestion_path:
            logger.info(
                "Static tool suggestions were written to %s",
                suggestion_path.relative_to(Config.PROJECT_ROOT).as_posix(),
            )
        return suggestion_path
    except Exception as exc:
        logger.exception(f"Static tool suggestion generation failed for Expected Result: {exc}")
        return None


def _expected_result_as_unsupported_step(static_result) -> ScenarioStep:
    failed_step_no = 0
    expected_result = ""
    failed_step_text = ""
    try:
        report = JiraBugClient.parse_automation_report(static_result.report_text)
        failed_step_no = report.failed_step_no or 0
        expected_result = report.expected_result or ""
        failed_step_text = report.failed_step_text or ""
    except Exception:
        pass

    detail = expected_result or static_result.error_message or static_result.summary
    action_context = f" | Action context: {failed_step_text}" if failed_step_text else ""
    return ScenarioStep(
        number=failed_step_no,
        text=f"Expected Result is not executable: {detail}{action_context}",
    )

# static_result = StaticExecutionResult(
#     status=StaticExecutionStatus.PASSED,
#     summary="Static execution passed after locator healing.",
#     app_name="demo_app",
#     matched_tools=["navigate_to_url", "click_element"],
#     page_url="https://example.com/login",
#     healing_attempted=True,
#     healing_patch={
#         "app_name": "demo_app",
#         "locator_key": "login_button",
#         "selector": "button[data-testid='sign-in-button']",
#         "reason": "Original selector did not match; MCP discovered a stable data-testid selector.",
#     },
# )   
def _create_bug_for_unresolved_static_failure(test_key: str, static_result, test_summary: str = "") -> dict:
    screenshot_path = _extract_error_screenshot_path(static_result.error_message)
    tried_selectors = ", ".join(static_result.tried_selectors) or "none"
    healing_patch = static_result.healing_patch or {}
    healed_selector = healing_patch.get("selector", "")

    # expected_assertion is True if the static failure was due to an Expected Result validation failure
    expected_assertion = (
        getattr(static_result, "failure_from_expected_validation", False)
        or static_result.failed_tool == "expected_result_validation"
    )
    if expected_assertion:
        parsed_expected = ""
        failed_step_no = None
        failed_step_text = ""
        try:
            parsed = JiraBugClient.parse_automation_report(static_result.report_text)
            parsed_expected = parsed.expected_result or ""
            failed_step_no = parsed.failed_step_no
            failed_step_text = parsed.failed_step_text or ""
        except Exception:
            pass
        report = FailureReport(
            xray_test_key=test_key,
            status="failed",
            summary="Expected Result assertion failed (static toolbox)",
            failed_step_no=failed_step_no,
            failed_step_text=failed_step_text
            or (
                f"Static tool: {static_result.failed_tool or 'unknown'} | "
                f"Locator key: {static_result.failed_locator_key}"
            ),
            expected_result=parsed_expected or "See scenario Expected Result for this step.",
            actual_result=static_result.error_message or static_result.summary,
            page_url=static_result.page_url,
            screenshot_path=screenshot_path,
            error_message=(
                f"{static_result.error_message} | created_at_utc="
                f"{datetime.now(timezone.utc).isoformat()}"
            ),
        )
        source = "static_expected_assertion"
    else:
        missing_target = _describe_missing_static_target(static_result) #missing target is a human-readable description of the missing element, derived from the failed locator key. For example, if the failed locator key is "login_button__primary", it will be described as "Login Button".
        recovery_status = (
            "MCP locator repair was attempted, but the static retry still failed."
            if static_result.healing_attempted
            else "MCP locator repair did not resolve the failure."
        )
        report = FailureReport(
            xray_test_key=test_key,
            status="failed",
            summary=f"{missing_target} could not be found",
            failed_step_text=(
                f"Static tool: {static_result.failed_tool or 'unknown'} | "
                f"Locator key: {static_result.failed_locator_key}"
            ),
            expected_result=f"{missing_target} should be visible and interactable.",
            actual_result=(
                f"{missing_target} could not be found. {recovery_status} "
                f"Tried selectors: {tried_selectors}. "
                f"Healed selector: {healed_selector or 'none'}."
            ),
            page_url=static_result.page_url,
            screenshot_path=screenshot_path,
            error_message=(
                f"{static_result.error_message} | created_at_utc="
                f"{datetime.now(timezone.utc).isoformat()}"
            ),
        )
        source = "static_locator_failure"

    bug = resolve_bug_action(report, Config.BUG_PROJECT_KEY, source=source, test_summary=test_summary)
    if bug.get("created"):
        logger.info("Bug issue created for unresolved failure: %s", bug.get("key"))
    elif bug.get("candidate"):
        logger.info("Bug candidate queued for manual review: %s", bug.get("id"))
    else:
        logger.warning("Bug creation skipped for unresolved static failure: %s", bug.get("error") or bug.get("status"))
    return bug


def _extract_error_screenshot_path(error_message: str) -> str:
    match = re.search(r"error_screenshot=([^\s|]+)", error_message or "")
    if not match:
        return ""
    path = Config.PROJECT_ROOT / match.group(1).strip()
    return str(path)


def _describe_missing_static_target(static_result) -> str:
    locator_key = static_result.failed_locator_key or "target element"
    readable = re.sub(r"__[a-z0-9_]+$", "", locator_key)
    readable = readable.replace("_", " ").strip()
    return readable.title() if readable else "Target element"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
