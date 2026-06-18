from autogen_agentchat.conditions import TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat

from Agents.agentFactory import AgentFactory
from Bug_Review.bug_review import empty_bug_result, resolve_bug_action
from Jira_Aut.jira_bug_client import FailureReport, JiraBugClient
from LLMs.modelClient import ModelClient
from Resources.config import Config
from Resources.logger_config import get_logger
from Resources.report_utils import (
    calculate_token_usage,
    extract_last_text_message,
    normalize_automation_report_text,
)

logger = get_logger("McpFullScenarioExecutor")
MCP_FULL_SCENARIO_COMPLETED = "MCP_FULL_SCENARIO_COMPLETED"

async def execute_with_mcp_full_scenario(
    test_key: str,
    scenario_text: str,
    test_summary: str = "",
) -> tuple[int, dict]:
    # This is the non-static execution path: MCP receives the full scenario and runs it end-to-end.
    model_client = ModelClient.chat_model_client()
    factory = AgentFactory(model_client)
    playwright_agent = factory.create_playwright_agent(
        system_message=(
            "You are a Playwright MCP test execution agent.\n"
            "Run the provided Xray web scenario end-to-end in the browser.\n"
            "Use the browser tools directly and do not call the static toolbox.\n"
            "If a step fails, stop at the failure and report the exact failing step.\n"
            "At the end, return a JSON automation report between "
            f"{JiraBugClient.REPORT_START} and {JiraBugClient.REPORT_END}.\n"
            "JSON keys: xray_test_key, status, summary, failed_step_no, "
            "failed_step_text, expected_result, actual_result, page_url, "
            "screenshot_path, error_message.\n"
            "status must be either passed or failed. Do not wrap JSON in markdown fences.\n"
            f"After the report, write exactly: {MCP_FULL_SCENARIO_COMPLETED}."
        )
    )
    team = RoundRobinGroupChat(
        participants=[playwright_agent],
        termination_condition=TextMentionTermination(MCP_FULL_SCENARIO_COMPLETED),
    )
    task = (
        f"Xray test key: {test_key}\n\n"
        "Execute this scenario exactly as written:\n\n"
        f"{scenario_text}"
    )

    logger.info("Step 2: Running full scenario with Playwright MCP...")
    task_result = await team.run(task=task)
    usage = calculate_token_usage(task_result)
    logger.info(
        "MCP full scenario token usage | prompt_tokens=%s | completion_tokens=%s | total_tokens=%s",
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )

    raw_report = normalize_automation_report_text(extract_last_text_message(task_result))
    logger.debug("Final MCP automation report text:\n%s", raw_report)

    try:
        report = JiraBugClient.parse_automation_report(raw_report) # such as { "xray_test_key": "PROJ-123", "status": "failed", "summary": "Login button not working", ... }
    except Exception as exc:
        logger.exception("Could not parse MCP automation report: %s", exc)
        return 1, empty_bug_result()

    if not report.is_failed:
        logger.info("Step 3: Scenario passed via MCP server. No bug issue was created.")
        return 0, empty_bug_result()

    logger.error("MCP scenario execution failed: %s", report.summary)
    return 1, _create_bug_for_mcp_failure(report, test_summary=test_summary)


def _create_bug_for_mcp_failure(report: FailureReport, test_summary: str = "") -> dict:
    # MCP failures already arrive as a generic FailureReport, so bug creation can stay simple.
    bug = resolve_bug_action(
        report,
        Config.BUG_PROJECT_KEY,
        source="mcp_full_scenario_failure",
        test_summary=test_summary,
    )
    if bug.get("created"):
        logger.info("Bug issue created for failed scenario: %s", bug.get("key"))
    elif bug.get("candidate"):
        logger.info("Bug candidate queued for manual review: %s", bug.get("id"))
    elif bug.get("status") == "disabled":
        logger.info("Bug creation is disabled for MCP failures.")
    else:
        logger.warning("Bug creation skipped for MCP failure: %s", bug.get("error") or bug.get("status"))
    return bug
