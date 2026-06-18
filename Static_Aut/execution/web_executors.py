import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import async_playwright

from Jira_Aut.jira_bug_client import JiraBugClient
from Resources.config import Config
from Resources.logger_config import get_logger
from Static_Aut.execution import expected_validation, locator_resolver, parsing
from Static_Aut.execution.context import StepContext
from Static_Aut.toolbox.static_toolbox import (
    ScenarioStep,
    StaticExecutionResult,
    StaticExecutionStatus,
    StaticLocatorError,
    analyze_web_static_plan,
    parse_expected_results,
    parse_scenario_steps,
    resolve_web_tool_names,
)
from Static_Aut.profiles.app_profiles import app_profile_registry


LOGGER = get_logger("StaticWebExecutor")
EXPECTED_VALIDATION_HEALABLE_TOOLS = {
    # Expected Result assertions in this set still depend on resolving a concrete
    # page element. If they fail, the problem may be a stale/missing locator, so
    # MCP locator healing is allowed to run.
    #
    # Example:
    #   Expected Result: '"Welcome" button is visible'
    #   validation_tool: "assert_element_visible"
    #   failure_from_expected_validation becomes False, so locator healing can run.
    "assert_element_visible",
    "assert_image_visible",
    "assert_checkbox_checked",
    "assert_checkbox_unchecked",
    "assert_dropdown_selected",
    "assert_input_value",
}

def _report_text(payload: dict) -> str:
    # JiraBugClient expects automation reports to be wrapped in these markers.
    return (
        f"{JiraBugClient.REPORT_START}\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"{JiraBugClient.REPORT_END}"
    )


def _build_pass_report(test_key: str, summary: str, page_url: str, screenshot_path: str) -> str:
    # A passed static run still returns a parseable automation report for the caller.
    payload = {
        "xray_test_key": test_key,
        "status": "passed",
        "summary": summary,
        "failed_step_no": None,
        "failed_step_text": "",
        "expected_result": "",
        "actual_result": "Static web executor completed the scenario successfully.",
        "page_url": page_url,
        "screenshot_path": screenshot_path,
        "error_message": "",
        "execution_engine": "static_web_executor",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return _report_text(payload)


def _build_failure_report(
    test_key: str,
    step: ScenarioStep | None,
    tool_name: str,
    error_message: str,
    page_url: str,
    screenshot_path: str,
    expected_result: str = "",
) -> str:
    # Failed reports are consumed later when the Jira bug client creates an issue.
    payload = {
        "xray_test_key": test_key,
        "status": "failed",
        "summary": f"Static web tool failed: {tool_name or 'unknown'}",
        "failed_step_no": step.number if step else None,
        "failed_step_text": step.text if step else "",
        "expected_result": expected_result or "The generic static web tool should complete the requested step.",
        "actual_result": error_message,
        "page_url": page_url,
        "screenshot_path": screenshot_path,
        "error_message": (
            f"{error_message} | error_screenshot={_relative_path(screenshot_path)} "
            f"| created_at_utc={datetime.now(timezone.utc).isoformat()}"
        ),
    }
    return _report_text(payload)


async def execute_static_web_flow(
    test_key: str,
    scenario_text: str,
    tool_overrides: dict[int, str] | None = None,  # when no matching with static tool, llm is triggered and produce an override like tool_overrides={2: "click_element", 4: "assert_text_visible"},
) -> StaticExecutionResult:
    # Execute every numbered scenario step with the generic Playwright toolbox.
    tool_overrides = tool_overrides or {}
    # "analysis" identifies if there is unsupported steps that cannot be executed with the current generic toolset, such as "Upload a file named resume.pdf to the application" which requires a file path extraction that is not yet implemented in the locator resolver. If there are unsupported steps, the execution will be marked as UNSUPPORTED and the unsupported steps will be included in the result for visibility.
    analysis = analyze_web_static_plan(scenario_text, tool_overrides=tool_overrides)
    # Resolve the app profile once so all locator lookups use the same app-specific locator file.
    app_name = app_profile_registry.resolve_for_scenario(scenario_text).app_name
    analysis.app_name = app_name
    if analysis.status == StaticExecutionStatus.UNSUPPORTED:
        return analysis

    # Split the normalized scenario into executable action steps and expected-result assertions.
    steps = parse_scenario_steps(scenario_text)
    expected_results = parse_expected_results(scenario_text) 
    # expected results 
    #     {
    #     1: [
    #         "Login page is displayed",
    #         "Username field is visible",
    #         "Password field is visible"
    #     ],
    #     2: [
    #         "Dashboard is displayed"
    #     ]
    #     }

    matched_tools = analysis.matched_tools
    screenshot_path = ""
    page_url = ""
    last_step: ScenarioStep | None = None
    last_tool = ""
    last_expected_result = ""
    failure_from_expected_validation = False

    try:
        async with async_playwright() as playwright:
            # Create an isolated browser context for this test execution.
            browser = await playwright.chromium.launch(headless=Config.STATIC_PLAYWRIGHT_HEADLESS)
            context = await browser.new_context()
            page = await context.new_page()
            # steps asre such as [ScenarioStep(number=1, text="Open the login page at https://example.com/login"), ScenarioStep(number=2, text="Fill in the username field with 'testuser'"), ScenarioStep(number=3, text="Fill in the password field with 'password123'"), ScenarioStep(number=4, text="Click the login button"), ...]
            try:
                for step in steps:
                    # Reset this flag for each primary action; Expected Result checks below may set it again.
                    failure_from_expected_validation = False
                    tool_names = resolve_web_tool_names(
                        step.text,
                        forced_tool_name=tool_overrides.get(step.number, ""),
                    )
                    if not tool_names:
                        # Static mode cannot execute this step unless LLM routing provides an override.
                        return StaticExecutionResult(
                            status=StaticExecutionStatus.UNSUPPORTED,
                            summary=f"No generic static tool matched step {step.number}.",
                            matched_tools=matched_tools,
                            unsupported_steps=[step],
                            tool_overrides=tool_overrides,
                            page_url=page.url,
                            app_name=app_name,
                        )

                    # The first matched tool is treated as the deterministic route.
                    last_step = step
                    last_tool = tool_names[0]
                    expected_result_items = expected_results.get(step.number, [])
                    last_expected_result = "\n".join(expected_result_items)
                    await _execute_tool(page, StepContext(step, last_tool, test_key, app_name)) # runs the primary tool for the step, which may be overridden by the LLM router if the static analysis found a better match for the step text. For example, if the step text is "Click the login button" but the static tool matching only finds "assert_element_visible" as a match, the LLM router might override it to "click_element" which is a better fit for the action described in the step. The tool execution may raise a StaticLocatorError if it fails to find or interact with the target element, which will be caught later to build a failure report with details about the error and what was tried.

                    for expected_result_item in expected_result_items:
                        # One Expected Result can expand into multiple concrete assertions.
                        # Example: '"Login" and "Register" buttons are visible'.
                        for expanded_expected_result in expected_validation.expand_expected_result_items(expected_result_item
                        ):
                            # Convert human-readable Expected Result text into an executable validation step.
                            validation_step, validation_tool = expected_validation.build_expected_validation_step(
                                step,
                                expanded_expected_result,
                            )
                            if not validation_step or not validation_tool:
                                if expected_validation.should_skip_expected_validation(expanded_expected_result):
                                    # Procedural Expected Results are ignored because they describe actions,
                                    # not observable outcomes.
                                    LOGGER.error(
                                        "Skipping non-assertive Expected Result for step %s: %s",
                                        step.number,
                                        expanded_expected_result,
                                    )
                                    continue
                                message = (
                                    f"Expected Result for step {step.number} is not in an executable format: "
                                    f"{expanded_expected_result!r}. Use explicit text, URL, title, or visible element assertions."
                                )
                                
                                page_url = page.url

                                # returns screenshot path if page is not None, otherwise returns empty string
                                screenshot_path = await _safe_error_screenshot(page, test_key)
                                return StaticExecutionResult(
                                    status=StaticExecutionStatus.FAILED,
                                    summary=message,
                                    matched_tools=matched_tools,
                                    error_message=message,
                                    report_text=_build_failure_report(
                                        test_key,
                                        step,
                                        "expected_result_validation",
                                        message,
                                        page_url,
                                        screenshot_path,
                                        expected_result=expanded_expected_result,
                                    ),
                                    failed_tool="expected_result_validation",
                                    failed_locator_key="",
                                    page_url=page_url,
                                    tool_overrides=tool_overrides,
                                    app_name=app_name,
                                    failure_from_expected_validation=True,
                                )

                            last_step = validation_step
                            last_tool = validation_tool
                            last_expected_result = expanded_expected_result
                            # If the validation tool is not locator-healable, treat
                            # the failure as an Expected Result assertion failure and
                            # skip MCP locator healing later.
                            #
                            # Example not healable:
                            #   validation_tool="assert_url_contains"
                            #   failure_from_expected_validation=True
                            #
                            # Example healable:
                            #   validation_tool="assert_element_visible"
                            #   failure_from_expected_validation=False
                            failure_from_expected_validation = validation_tool not in EXPECTED_VALIDATION_HEALABLE_TOOLS
                            await _execute_tool(
                                page,
                                StepContext(validation_step, validation_tool, test_key, app_name),
                            )

                page_url = page.url
                # Capture a final screenshot for the structured pass report.
                screenshot_path = await _take_screenshot(page, test_key, "passed")
                return StaticExecutionResult(
                    status=StaticExecutionStatus.PASSED,
                    summary="Static web executor completed all generic web steps.",
                    matched_tools=matched_tools,
                    report_text=_build_pass_report(
                        test_key,
                        "Static web executor completed all generic web steps.",
                        page_url,
                        screenshot_path,
                    ),
                    page_url=page_url,
                    tool_overrides=tool_overrides,
                    app_name=app_name,
                )
            finally:
                # Always release browser resources, even when an action or assertion fails.
                await context.close()
                await browser.close()
    except StaticLocatorError as exc:
        # StaticLocatorError carries structured tool/locator details, so use them directly.
        # Typical cases: target element not found, all saved/fallback selectors failed,
        # input value mismatch, checkbox/dropdown state mismatch, or a text/element
        # visibility assertion failed.
        page_url = getattr(locals().get("page", None), "url", page_url)
        screenshot_path = await _safe_error_screenshot(locals().get("page"), test_key)
        failure_from_expected_validation = (
            failure_from_expected_validation or getattr(exc, "assertion_failed", False)
        )
        return StaticExecutionResult(
            status=StaticExecutionStatus.FAILED,
            summary=str(exc),
            matched_tools=matched_tools,
            error_message=str(exc),
            report_text=_build_failure_report(
                test_key,
                last_step,
                exc.tool_name,
                str(exc),
                page_url,
                screenshot_path,
                expected_result=last_expected_result,
            ),
            failed_tool=exc.tool_name,
            failed_locator_key=exc.locator_key,
            tried_selectors=exc.tried_selectors,
            page_url=page_url,
            # Short HTML snapshot from the failure page, used for debugging and MCP locator healing.
            html_excerpt=await _safe_html_excerpt(locals().get("page")),
            tool_overrides=tool_overrides,
            app_name=app_name,
            failure_from_expected_validation=failure_from_expected_validation,
        )
    except Exception as exc:
        # Unexpected errors do not carry locator metadata; report the last known step/tool context.
        page_url = getattr(locals().get("page", None), "url", page_url)
        screenshot_path = await _safe_error_screenshot(locals().get("page"), test_key)
        error_message = f"{type(exc).__name__}: {exc}"
        return StaticExecutionResult(
            status=StaticExecutionStatus.FAILED,
            summary=error_message,
            matched_tools=matched_tools,
            error_message=error_message,
            report_text=_build_failure_report(
                test_key,
                last_step,
                last_tool,
                error_message,
                page_url,
                screenshot_path,
                expected_result=last_expected_result,
            ),
            failed_tool=last_tool,
            failed_locator_key=parsing.locator_key(last_tool, last_step.text if last_step else ""),
            page_url=page_url,
            # Short HTML snapshot from the failure page, used for debugging and MCP locator healing.
            html_excerpt=await _safe_html_excerpt(locals().get("page")),
            tool_overrides=tool_overrides,
            app_name=app_name,
            failure_from_expected_validation=failure_from_expected_validation,
        )


async def _execute_tool(page: Any, context: StepContext) -> None:
    # Keep the dispatcher explicit so adding/removing tools stays easy to review.
    # dispatcher matches the most likely intended tool based on the step text, but if multiple tools match, the order of checks in the dispatcher determines which one is chosen as the primary execution path. This allows for prioritizing more specific tools over more general ones when there are overlaps in their matching criteria.
    handlers: dict[str, Callable[[Any, StepContext], Awaitable[None]]] = {
        "navigate_to_url": _navigate_to_url,
        "reload_page": _reload_page,
        "go_back": _go_back,
        "go_forward": _go_forward,
        "wait_for_page_load": _wait_for_page_load,
        "wait_for_element": _wait_for_element,
        "wait_for_text": _wait_for_text,
        "wait": _wait,
        "click_element": _click_element,
        "double_click_element": _double_click_element,
        "right_click_element": _right_click_element,
        "hover_element": _hover_element,
        "focus_element": _focus_element,
        "fill_input": _fill_input,
        "clear_input": _clear_input,
        "type_text": _type_text,
        "press_key": _press_key,
        "check_checkbox": _check_checkbox,
        "uncheck_checkbox": _uncheck_checkbox,
        "select_option": _select_option,
        "upload_file": _upload_file,
        "scroll_page": _scroll_page,
        "scroll_until_end": _scroll_until_end,
        "scroll_to_element": _scroll_to_element,
        "drag_and_drop": _drag_and_drop,
        "find_text": _find_text,
        "assert_text_visible": _assert_text_visible,
        "assert_text_not_visible": _assert_text_not_visible,
        "assert_image_visible": _assert_image_visible,
        "assert_image_not_visible": _assert_image_not_visible,
        "assert_element_visible": _assert_element_visible,
        "assert_element_hidden": _assert_element_hidden,
        "assert_checkbox_checked": _assert_checkbox_checked,
        "assert_checkbox_unchecked": _assert_checkbox_unchecked,
        "assert_dropdown_selected": _assert_dropdown_selected,
        "assert_url_contains": _assert_url_contains,
        "assert_title_contains": _assert_title_contains,
        "assert_input_value": _assert_input_value,
        "assert_table_column_contains_value": _assert_table_column_contains_value,
        "assert_table_columns_populated": _assert_table_columns_populated,
        "get_text": _get_text,
        "get_attribute": _get_attribute,
        "get_input_value": _get_input_value,
        "count_elements": _count_elements,
        "accept_dialog": _accept_dialog,
        "dismiss_dialog": _dismiss_dialog,
        "take_screenshot": _take_screenshot_step,
    }
    handler = handlers.get(context.tool_name)
    if handler is None:
        raise StaticLocatorError(
            "No executor is registered for the selected generic tool.",
            context.tool_name,
            parsing.locator_key(context.tool_name, context.step.text),
        )
    LOGGER.info("Static step %s -> %s | %s", context.step.number, context.tool_name, context.step.text)
    await handler(page, context)


async def _navigate_to_url(page: Any, context: StepContext) -> None:
    # Open the first URL found in the step text.
    url = parsing.extract_url(context.step.text)
    if not url:
        raise locator_resolver.tool_error(context, "No URL was found in the navigation step.")
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)


async def _reload_page(page: Any, context: StepContext) -> None:
    # Reload keeps the current browser URL and refreshes the document.
    await page.reload(wait_until="domcontentloaded")


async def _go_back(page: Any, context: StepContext) -> None:
    # Browser-history navigation mirrors a real user's Back button.
    await page.go_back(wait_until="domcontentloaded")


async def _go_forward(page: Any, context: StepContext) -> None:
    # Browser-history navigation mirrors a real user's Forward button.
    await page.go_forward(wait_until="domcontentloaded")


async def _wait_for_page_load(page: Any, context: StepContext) -> None:
    # Prefer networkidle for tests, but fall back to domcontentloaded if the site keeps connections open.
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)


async def _wait_for_element(page: Any, context: StepContext) -> None:
    # Resolve the target with the same generic locator strategy used by click/assert tools.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Element did not become visible: {exc}", tried)


async def _wait_for_text(page: Any, context: StepContext) -> None:
    # Text waits use Playwright's text engine so they work across common markup.
    text = parsing.extract_text_target(context.step.text)
    if not text:
        raise locator_resolver.tool_error(context, "No target text was found for wait_for_text.")
    await _wait_for_any_visible_text(page, text, timeout_ms=10_000)


async def _wait(page: Any, context: StepContext) -> None:
    # Fixed waits are capped to avoid hiding real slowness for too long.
    seconds = min(_extract_first_number(context.step.text, default=1), 10)
    await page.wait_for_timeout(int(seconds * 1000))


async def _click_element(page: Any, context: StepContext) -> None:
    # Click supports selectors, visible text, labels, placeholders, and role/name targets.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await _click_any_visible_locator(page, locator, timeout_ms=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not click target: {exc}", tried)


async def _double_click_element(page: Any, context: StepContext) -> None:
    # Double click uses the same generic target resolution as a normal click.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await _click_any_visible_locator(page, locator, timeout_ms=10_000, double=True)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not double-click target: {exc}", tried)


async def _right_click_element(page: Any, context: StepContext) -> None:
    # Right click opens context menus or element-specific browser actions.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await _click_any_visible_locator(page, locator, timeout_ms=10_000, button="right")
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not right-click target: {exc}", tried)


async def _click_any_visible_locator(
    page: Any,
    locator: Any,
    timeout_ms: int,
    button: str = "left",
    double: bool = False,
) -> None:
    # Generic text locators may match hidden submenu templates before the visible item.
    # Example: "Entertainment Statistics" can exist in both a closed/hidden dropdown
    # and the currently opened dropdown. Click the first visible match, not DOM match 0.
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_count = 0

    while time.monotonic() < deadline:
        last_count = await locator.count()
        for index in range(last_count):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            if double:
                await candidate.dblclick(timeout=timeout_ms)
            else:
                await candidate.click(button=button, timeout=timeout_ms)
            return
        await page.wait_for_timeout(250)

    raise TimeoutError(
        f"Timed out after {timeout_ms}ms waiting for any visible clickable match. "
        f"Matched elements: {last_count}."
    )


async def _hover_element(page: Any, context: StepContext) -> None:
    # Hover is useful for menus and tooltips that reveal content on mouseover.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.hover(timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not hover target: {exc}", tried)


async def _focus_element(page: Any, context: StepContext) -> None:
    # Focus prepares a field or control for keyboard input.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.focus(timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not focus target: {exc}", tried)


async def _fill_input(page: Any, context: StepContext) -> None:
    # Fill replaces the current value of the resolved input-like element.
    value = parsing.resolve_runtime_input_value(parsing.extract_value(context.step.text))
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.fill(value, timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not fill input: {exc}", tried)


async def _clear_input(page: Any, context: StepContext) -> None:
    # Clearing is implemented as fill("") because it works for inputs and textareas.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.fill("", timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not clear input: {exc}", tried)


async def _type_text(page: Any, context: StepContext) -> None:
    # Typing appends text into the active element unless a target field can be resolved.
    value = parsing.resolve_runtime_input_value(parsing.extract_value(context.step.text))
    try:
        locator, _ = await locator_resolver.resolve_locator(page, context)
        await locator.first.type(value, timeout=10_000)
    except StaticLocatorError:
        await page.keyboard.type(value)


async def _press_key(page: Any, context: StepContext) -> None:
    # Keyboard keys are normalized to Playwright's expected key names.
    await page.keyboard.press(_extract_key(context.step.text))


async def _check_checkbox(page: Any, context: StepContext) -> None:
    # Check works for checkbox and radio inputs.
    locator, tried = await locator_resolver.resolve_locator(page, context)

    try:
        element = await _resolve_checkbox_control(locator.first, context, tried)
        if not await element.is_checked():
            await element.click(timeout=10_000, force=True)
    except Exception as exc:
        raise locator_resolver.tool_error(
            context,
            f"Could not interact with checkbox/radio: {exc}",
            tried
        )


async def _uncheck_checkbox(page: Any, context: StepContext) -> None:
    # Uncheck only applies to checkbox inputs.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        element = await _resolve_checkbox_control(locator.first, context, tried)
        if await element.is_checked():
            await element.click(timeout=10_000, force=True)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not uncheck target: {exc}", tried)


async def _select_option(page: Any, context: StepContext) -> None:
    # Select option chooses by label first, then by value if the label is not present.
    value = parsing.extract_dropdown_option(context.step.text)
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.select_option(label=value, timeout=10_000)
    except Exception:
        try:
            await locator.first.select_option(value=value, timeout=10_000)
        except Exception as exc:
            raise locator_resolver.tool_error(context, f"Could not select option: {exc}", tried)


async def _assert_dropdown_selected(page: Any, context: StepContext) -> None:
    quoted_values = parsing.extract_quoted_values(context.step.text)
    expected_value = quoted_values[-1].strip() if len(quoted_values) >= 2 else parsing.extract_dropdown_option(context.step.text)
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        selected_text = await locator.first.evaluate(
            """el => {
                if (!el || el.tagName.toLowerCase() !== 'select') {
                    throw new Error('Resolved target is not a select/dropdown control.');
                }
                const option = el.selectedOptions && el.selectedOptions[0];
                return option ? option.textContent.trim() : '';
            }"""
        )
        selected_value = await locator.first.input_value(timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read dropdown state: {exc}", tried)

    if expected_value.lower() not in {str(selected_text).lower(), str(selected_value).lower()}:
        raise locator_resolver.tool_error(
            context,
            f"Dropdown selected value mismatch. Expected '{expected_value}', got text '{selected_text}' / value '{selected_value}'.",
            tried,
            assertion_failed=True,
        )


async def _upload_file(page: Any, context: StepContext) -> None:
    # Upload expects a local path in the step text and sends it to a file input.
    file_path = _extract_file_path(context.step.text)
    if not file_path:
        raise locator_resolver.tool_error(context, "No file path was found for upload_file.")
    locator, tried = await locator_resolver.resolve_locator(page, context, fallback_selector="input[type='file']")
    # fallback_Selector is a default selector for file inputs, but if the user provided a more specific target, it will be tried first in _resolve_locator.
    #etc. The file path extraction should handle common phrasing like "Upload the file at 'C:\path\to\file.txt'" or "Set the file input to /path/to/file.txt". The locator resolution for uploads prioritizes the user's specified target but falls back to a generic file input selector if no specific target is found, since file inputs can be tricky to identify by visible text alone.
    try:
        await locator.first.set_input_files(file_path, timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not upload file: {exc}", tried)


async def _scroll_page(page: Any, context: StepContext) -> None:
    # Page scrolling supports top, bottom, up, and down directions.
    text = context.step.text.lower()
    if any(token in text for token in ("top", "yukari", "yukarÄ±")):
        await page.evaluate("window.scrollTo(0, 0)")
    elif any(token in text for token in ("bottom", "asagi", "aÅŸaÄŸÄ±")):
        await _scroll_until_page_end(page, context)
    elif "up" in text:
        await page.mouse.wheel(0, -800)
    else:
        await page.mouse.wheel(0, 800)


async def _scroll_until_end(page: Any, context: StepContext) -> None:
    await _scroll_until_page_end(page, context)


async def _scroll_to_element(page: Any, context: StepContext) -> None:
    # Text scrolls are common in long content pages; try them directly before the
    # generic form/control locator path so offscreen paragraphs do not fail early.
    target_text = parsing.extract_text_target(context.step.text)
    if target_text and not parsing.looks_like_selector(target_text):
        tried = [f"text={target_text}"]
        tried.append(f"dom_text={target_text}")
        if await _scroll_until_text_found(page, context, target_text):
            return
        raise locator_resolver.tool_error(
            context,
            f"Could not find target text after scrolling the page. Current URL: {page.url}",
            tried,
        )

    # Scroll a resolved element into view without clicking it.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.scroll_into_view_if_needed(timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not scroll to target: {exc}", tried)


async def _visible_viewport_contains_text(page: Any, target_text: str) -> bool:
    return await page.evaluate(
        """target => {
            const normalize = value => (value || "").replace(/\\s+/g, " ").trim().toLowerCase();
            const wanted = normalize(target);
            if (!wanted) {
                return false;
            }
            const nodes = Array.from(document.body.querySelectorAll("*"));
            const matches = nodes.filter(el => {
                if (!normalize(el.textContent).includes(wanted)) {
                    return false;
                }
                const style = window.getComputedStyle(el);
                if (style.visibility === "hidden" || style.display === "none") {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 0 &&
                    rect.height > 0 &&
                    rect.bottom >= 0 &&
                    rect.top <= window.innerHeight &&
                    rect.right >= 0 &&
                    rect.left <= window.innerWidth;
            });
            if (!matches.length) {
                return false;
            }
            const element = matches.find(el => !Array.from(el.children).some(
                child => normalize(child.textContent).includes(wanted)
            )) || matches[matches.length - 1];
            element.scrollIntoView({ block: "center", inline: "nearest" });
            return true;
        }""",
        target_text,
    )


async def _scroll_until_text_found(page: Any, context: StepContext, target_text: str) -> bool:
    await page.evaluate("window.scrollTo(0, 0)")
    attempt = 0
    while True:
        scroll_y = await page.evaluate("window.scrollY")
        found_dom_text = await _visible_viewport_contains_text(page, target_text)
        LOGGER.info(
            "Static scroll viewport text lookup | step=%s | attempt=%s | url=%s | scroll_y=%s | found=%s | target=%s",
            context.step.number,
            attempt,
            page.url,
            scroll_y,
            found_dom_text,
            target_text,
        )
        if found_dom_text:
            return True

        scroll_state = await _scroll_down_one_viewport(page)
        if scroll_state["after"] == scroll_state["before"]:
            break
        attempt += 1
        await page.wait_for_timeout(250)
    contains_text, excerpt = await _page_text_debug(page, target_text)
    LOGGER.info(
        "Static scroll text not found after page end | step=%s | url=%s | full_page_contains=%s | excerpt=%s",
        context.step.number,
        page.url,
        contains_text,
        excerpt,
    )
    return False


async def _page_text_debug(page: Any, target_text: str) -> tuple[bool, str]:
    return await page.evaluate(
        """target => {
            const normalize = value => (value || "").replace(/\\s+/g, " ").trim();
            const haystack = normalize(document.body ? document.body.innerText : "");
            const wanted = normalize(target);
            const lowerHaystack = haystack.toLowerCase();
            const lowerWanted = wanted.toLowerCase();
            const index = lowerWanted ? lowerHaystack.indexOf(lowerWanted) : -1;
            if (index >= 0) {
                return [true, haystack.slice(Math.max(0, index - 120), index + wanted.length + 120)];
            }
            const firstWord = lowerWanted.split(" ")[0] || "";
            const firstWordIndex = firstWord ? lowerHaystack.indexOf(firstWord) : -1;
            if (firstWordIndex >= 0) {
                return [false, haystack.slice(Math.max(0, firstWordIndex - 120), firstWordIndex + 240)];
            }
            return [false, haystack.slice(0, 500)];
        }""",
        target_text,
    )


async def _scroll_until_page_end(page: Any, context: StepContext) -> None:
    previous_scroll_y = -1
    attempt = 0
    while True:
        scroll_state = await _scroll_down_one_viewport(page)
        LOGGER.info(
            "Static scroll until end | step=%s | attempt=%s | url=%s | before=%s | after=%s | max=%s",
            context.step.number,
            attempt,
            page.url,
            scroll_state["before"],
            scroll_state["after"],
            scroll_state["max"],
        )
        if scroll_state["after"] == previous_scroll_y or scroll_state["after"] >= scroll_state["max"]:
            break
        previous_scroll_y = scroll_state["after"]
        attempt += 1
        await page.wait_for_timeout(250)


async def _scroll_down_one_viewport(page: Any) -> dict[str, int]:
    return await page.evaluate(
        """() => {
            const doc = document.documentElement;
            const body = document.body;
            const before = window.scrollY;
            const viewport = window.innerHeight;
            const height = Math.max(
                body.scrollHeight,
                body.offsetHeight,
                doc.clientHeight,
                doc.scrollHeight,
                doc.offsetHeight
            );
            const max = Math.max(0, height - viewport);
            window.scrollBy(0, Math.max(350, Math.floor(viewport * 0.8)));
            return { before, after: window.scrollY, max };
        }"""
    )


async def _drag_and_drop(page: Any, context: StepContext) -> None: # etc. "Drag the 'Username' field onto the 'Password' field" or "Drag the item with text 'A' and drop it on the item with text 'B'".
    # Drag-and-drop expects two quoted values: source and target.
    values = parsing.extract_quoted_values(context.step.text)
    if len(values) < 2:
        raise locator_resolver.tool_error(context, "Drag and drop requires source and target text or selectors.")
    source = page.locator(values[0]) if parsing.looks_like_selector(values[0]) else page.get_by_text(values[0], exact=False)
    target = page.locator(values[1]) if parsing.looks_like_selector(values[1]) else page.get_by_text(values[1], exact=False)
    await source.first.drag_to(target.first, timeout=10_000)


async def _find_text(page: Any, context: StepContext) -> None:
    # Finding text is a non-mutating visibility check.
    await _assert_text_visible(page, context)


async def _assert_text_visible(page: Any, context: StepContext) -> None:
    # Assert text appears somewhere visible on the current page.
    text = parsing.extract_text_target(context.step.text)
    if not text:
        raise locator_resolver.tool_error(context, "No target text was found for assert_text_visible.")
    try:
        await _wait_for_any_visible_text(page, text, timeout_ms=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Text was not visible: {exc}", [f"text={text}"])


async def _assert_text_not_visible(page: Any, context: StepContext) -> None:
    # Assert text is absent or hidden.
    text = parsing.extract_text_target(context.step.text)
    if not text:
        raise locator_resolver.tool_error(context, "No target text was found for assert_text_not_visible.")
    try:
        await _wait_until_no_visible_text(page, text, timeout_ms=5_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Text was still visible: {exc}", [f"text={text}"])


async def _wait_for_any_visible_text(page: Any, text: str, timeout_ms: int) -> None:
    # Playwright's get_by_text(...).first waits only for the first DOM matchh.
    # Example: "System" can match a hidden submenu item before a visible dropdown item.
    # Treat the assertion as passed when any matching text node is visible.
    locator = page.get_by_text(text, exact=False) # Get a locator for all elements containing the target text, allowing for partial matches. This locator will be used to check the visibility of each matching element on the page.
    deadline = time.monotonic() + (timeout_ms / 1000) # Convert ms to seconds for monotonic time comparison.
    last_count = 0

    while time.monotonic() < deadline: 
        last_count = await locator.count() # Count the number of elements matching the text locator. This is done on each iteration to account for dynamic content changes that may add or remove matching elements from the DOM.
        for index in range(last_count):
            if await locator.nth(index).is_visible():
                return
        await page.wait_for_timeout(250)

    raise TimeoutError(
        f'Timed out after {timeout_ms}ms waiting for any visible text match: "{text}". '
        f"Matched elements: {last_count}."
    )


async def _wait_until_no_visible_text(page: Any, text: str, timeout_ms: int) -> None:
    # Negative text assertions must also inspect every match. If the first match is
    # hidden but a later match is visible, the text is still visible on the page.
    locator = page.get_by_text(text, exact=False)
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_visible_count = 0

    while time.monotonic() < deadline:
        last_visible_count = 0
        count = await locator.count()
        for index in range(count):
            if await locator.nth(index).is_visible():
                last_visible_count += 1
                break # En az bir tane görünür bulduysak, "görünür değil" şartı bozulmuştur, beklemeye devam.

        if last_visible_count == 0:
            return # Hiçbir eşleşme görünür değil (veya hiç eşleşme yok), başarıyla dön.
        await page.wait_for_timeout(250)

    raise TimeoutError(
        f'Timed out after {timeout_ms}ms waiting for text to be hidden: "{text}". '
        f"Visible matches: {last_visible_count}."
    )


def _css_attr_substring(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _target_variants(value: str) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    variants = {
        raw,
        raw.lower(),
        raw.replace(" ", "-"),
        raw.replace(" ", "_"),
        raw.replace(" ", ""),
        raw.lower().replace(" ", "-"),
        raw.lower().replace(" ", "_"),
        raw.lower().replace(" ", ""),
    }
    return [variant for variant in variants if len(variant.strip()) >= 3]


def _extract_image_assert_target(text: str) -> str:
    # Quoted string is treated as accessible name / alt substring for the image.
    quoted = parsing.extract_quoted_values(text)
    if quoted:
        return quoted[0].strip()
    match = re.search(
        r"\balt\s*(?:text\s*)?[:=]\s*[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    drop = re.sub(
        r"\b(verify|assert|check|ensure|see|that|the|a|an|is|are|was|were|must|should|be|"
        r"correctly|successfully|fully|partially|user|can|will|"
        r"visible|displayed|shown|appear|appears|appearing|hidden|not|page|screen|"
        r"logo|image|icon|picture|grafik|gÃ¶rsel|resim|gorsel|"
        r"with|for|on|at)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    drop = re.sub(r"\s+", " ", drop).strip(" .:-")
    return drop if len(drop) >= 2 else ""


def _accessible_name_regex_fragment(fragment: str) -> re.Pattern[str]:
    # Playwright matches the pattern against the full accessible name; allow substring match.
    return re.compile(f".*{re.escape(fragment)}.*", re.IGNORECASE | re.DOTALL)


def _image_locator_strategies(page: Any, target: str) -> list[tuple[str, Any]]:
    """Resolve logos/images: many sites use SVG, class/id, src=..., or wrap in <a> without <img alt>."""
    if not target.strip():
        media_in_branding = (
            "header img, [role=banner] img, nav img, "
            '[class*="logo"] img, img[class*="logo"], img[src*="logo"], img[src*="Logo"], '
            '[class*="logo"] svg, [class*="brand"] img, '
            'a[class*="logo"] img, [id*="logo"] img, '
            'header svg, nav svg, picture img'
        )
        branding_root = (
            '[class*="logo"], [id*="logo"], .logo, [data-testid*="logo"], [data-test*="logo"]'
        )
        # CSS-only / component shells often have no <img>; match the branding container first.
        return [
            (branding_root, page.locator(branding_root)),
            (media_in_branding, page.locator(media_in_branding)),
        ]

    safe = _css_attr_substring(target)
    name_re = _accessible_name_regex_fragment(target)
    text_in_logo_shell = re.compile(re.escape(target), re.IGNORECASE)
    strategies: list[tuple[str, Any]] = [
        (f'role=img,name~/{target}/i', page.get_by_role("img", name=name_re)),
        (f"alt~={target}", page.get_by_alt_text(target, exact=False)),
        (f'img[alt*="{safe}"]', page.locator(f'img[alt*="{safe}"]')),
        (f'img[src*="{safe}"]', page.locator(f'img[src*="{safe}"]')),
        (f'img[src*="{safe.lower()}"]', page.locator(f'img[src*="{safe.lower()}"]')),
        (f'img[title*="{safe}"]', page.locator(f'img[title*="{safe}"]')),
        (f'svg[aria-label*="{safe}"]', page.locator(f'svg[aria-label*="{safe}"]')),
        (f'a[aria-label*="{safe}"]', page.locator(f'a[aria-label*="{safe}"]')),
        (f'[aria-label*="{safe}"]', page.locator(f'[aria-label*="{safe}"]')),
        (
            "logo-shell+has_text",
            page.locator(
                '[class*="logo"], [id*="logo"], .logo, [data-testid*="logo"], header, [role="banner"], nav'
            ).filter(has_text=text_in_logo_shell),
        ),
        (
            "xpath=//nav//img[contains(@id,'logo') or contains(@class,'logo') or contains(@src,'logo')]",
            page.locator(
                "xpath=//nav//img[contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logo') "
                "or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logo') "
                "or contains(translate(@src,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logo')]"
            ),
        ),
        (
            "xpath=//*[@id='logo' or @id='logoMini' or contains(@id,'logo')]",
            page.locator(
                "xpath=//*[@id='logo' or @id='logoMini' or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logo')]"
            ),
        ),
    ]
    for variant in _target_variants(target):
        safe_variant = _css_attr_substring(variant)
        strategies.append(
            (f'img[src*="{safe_variant}"]', page.locator(f'img[src*="{safe_variant}"]'))
        )
    return strategies


async def _resolve_fuzzy_logo_locator(page: Any, target: str) -> tuple[Any, str] | None:
    base_selector = (
        'img, svg, [role="img"], a, [class*="logo"], [id*="logo"], '
        '[data-testid*="logo"], [data-test*="logo"], nav img, header img'
    )
    locator = page.locator(base_selector)
    normalized_target = _normalize_match_text(target)
    if not normalized_target:
        return None
    count = await locator.count()
    for idx in range(min(count, 250)):
        candidate = locator.nth(idx)
        try:
            if not await candidate.is_visible():
                continue
            blob = await candidate.evaluate(
                """el => [
                    el.id || "",
                    el.className || "",
                    el.getAttribute("src") || "",
                    el.getAttribute("alt") || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.textContent || "",
                ].join(" ")"""
            )
        except Exception:
            continue
        normalized_blob = _normalize_match_text(str(blob))
        if not normalized_blob:
            continue
        if normalized_target in normalized_blob or normalized_blob in normalized_target:
            return candidate, f"fuzzy_logo_match[{idx}]"
    return None


async def _assert_image_visible(page: Any, context: StepContext) -> None:
    text = _extract_image_assert_target(context.step.text)
    strategies = _image_locator_strategies(page, text)
    tried: list[str] = []
    last_error: Exception | None = None
    for description, locator in strategies:
        tried.append(description)
        try:
            if await locator.count() < 1:
                continue
            await locator.first.wait_for(state="visible", timeout=10_000)
            return
        except Exception as exc:
            last_error = exc
            continue
    fuzzy = await _resolve_fuzzy_logo_locator(page, text)
    if fuzzy:
        locator, description = fuzzy
        tried.append(description)
        try:
            await locator.wait_for(state="visible", timeout=10_000)
            return
        except Exception as exc:
            last_error = exc
    detail = f" {last_error}" if last_error else ""
    raise locator_resolver.tool_error(
        context,
        f"No matching visible image found (alt/role/header/img).{detail}",
        tried,
    )


async def _assert_image_not_visible(page: Any, context: StepContext) -> None:
    text = _extract_image_assert_target(context.step.text)
    strategies = _image_locator_strategies(page, text)
    tried: list[str] = []
    for description, locator in strategies:
        tried.append(description)
        try:
            count = await locator.count()
        except Exception:
            continue
        if count < 1:
            continue
        try:
            await locator.first.wait_for(state="hidden", timeout=5_000)
            return
        except Exception as exc:
            raise locator_resolver.tool_error(context, f"Image was still visible: {exc}", tried)
    # No matching nodes is acceptable for "not visible".


async def _assert_element_visible(page: Any, context: StepContext) -> None:
    # Assert a resolved element is visible.
    await _wait_for_element(page, context)


async def _assert_element_hidden(page: Any, context: StepContext) -> None:
    # Assert a resolved element is hidden.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        await locator.first.wait_for(state="hidden", timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Element was still visible: {exc}", tried)


async def _assert_checkbox_checked(page: Any, context: StepContext) -> None:
    await _assert_checkbox_state(page, context, expected_checked=True)


async def _assert_checkbox_unchecked(page: Any, context: StepContext) -> None:
    await _assert_checkbox_state(page, context, expected_checked=False)


async def _assert_checkbox_state(page: Any, context: StepContext, expected_checked: bool) -> None:
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        element = await _resolve_checkbox_control(locator.first, context, tried)
        actual_checked = await element.is_checked()
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read checkbox/radio state: {exc}", tried)
    if actual_checked != expected_checked:
        expected_state = "checked" if expected_checked else "unchecked"
        actual_state = "checked" if actual_checked else "unchecked"
        raise locator_resolver.tool_error(
            context,
            f"Checkbox/radio state mismatch. Expected {expected_state}, got {actual_state}.",
            tried,
            assertion_failed=True,
        )


async def _resolve_checkbox_control(element: Any, context: StepContext, tried: list[str]) -> Any:
    await element.wait_for(state="visible", timeout=10_000)
    tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
    input_type = (await element.get_attribute("type") or "").lower()
    role = (await element.get_attribute("role") or "").lower()
    if tag_name == "input" and input_type in {"checkbox", "radio"}:
        return element
    if role in {"checkbox", "radio"}:
        return element
    raise locator_resolver.tool_error(
        context,
        "Resolved target is not a checkbox/radio control.",
        tried,
    )


async def _assert_url_contains(page: Any, context: StepContext) -> None:
    # URL assertions compare against a quoted value or the last meaningful token.
    expected = parsing.extract_value(context.step.text)
    if expected not in page.url:
        raise locator_resolver.tool_error(context, f"Current URL does not contain '{expected}'. Actual URL: {page.url}")


async def _assert_title_contains(page: Any, context: StepContext) -> None:
    # Title assertions read the browser title and compare it as plain text.
    expected = parsing.extract_value(context.step.text)
    title = await page.title()
    if expected.lower() not in title.lower():
        raise locator_resolver.tool_error(context, f"Page title does not contain '{expected}'. Actual title: {title}")


async def _assert_input_value(page: Any, context: StepContext) -> None:
    # Compare the resolved input value with the expected text from the step.
    expected = parsing.resolve_runtime_input_value(parsing.extract_value(context.step.text))
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        actual = await locator.first.input_value(timeout=10_000)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read input value: {exc}", tried)
    if actual != expected:
        raise locator_resolver.tool_error(context, f"Input value mismatch. Expected '{expected}', got '{actual}'.", tried)


async def _assert_table_column_contains_value(page: Any, context: StepContext) -> None:
    # The canonical step contains the target column first and expected value second.
    quoted_values = parsing.extract_quoted_values(context.step.text)
    if len(quoted_values) < 2:
        raise locator_resolver.tool_error(
            context,
            "Table column value check requires a quoted column and expected value.",
            assertion_failed=True,
        )
    column_name = quoted_values[0]
    expected_value = parsing.resolve_runtime_input_value(quoted_values[-1])

    table, table_index = await _first_visible_table(page)
    if table is None:
        raise locator_resolver.tool_error(
            context,
            "No visible table or grid was found on the current page.",
            ["[role='grid']", "[role='table']", "table"],
            assertion_failed=True,
        )

    LOGGER.info(
        "Table column value check selected first visible table | index=%s | column=%s | expected=%s",
        table_index,
        column_name,
        expected_value,
    )

    page_number = 1
    total_checked_cells = 0
    mismatches: list[str] = []
    visited_signatures: set[str] = set()

    while True:
        headers = await _read_table_headers(table)
        header_lookup = {_table_key(header["name"]): index for index, header in enumerate(headers)}
        column_key = _table_key(column_name)
        if column_key not in header_lookup:
            raise locator_resolver.tool_error(
                context,
                f"Table column was not found: '{column_name}'. Available columns: "
                f"{[header['name'] for header in headers]}",
                assertion_failed=True,
            )

        page_signature = await _table_page_signature(table)
        if page_signature in visited_signatures:
            raise locator_resolver.tool_error(
                context,
                f"Table pagination returned to an already inspected page at page {page_number}.",
                assertion_failed=True,
            )
        visited_signatures.add(page_signature)

        checked_cells, page_mismatches = await _inspect_table_column_values(
            table,
            header_lookup[column_key],
            column_name,
            expected_value,
            page_number,
        )
        total_checked_cells += checked_cells
        mismatches.extend(page_mismatches)
        LOGGER.info(
            "Table column value check inspected page | page=%s | cells=%s | column=%s | expected=%s",
            page_number,
            checked_cells,
            column_name,
            expected_value,
        )

        next_button = await _find_table_next_button(table)
        if next_button is None or not await next_button.is_visible() or not await next_button.is_enabled():
            break

        await next_button.click()
        await _wait_for_table_page_change(page, table, page_signature, page_number, context)
        page_number += 1

    if total_checked_cells == 0:
        raise locator_resolver.tool_error(
            context,
            f"The selected table has no visible data cells in column '{column_name}'.",
            assertion_failed=True,
        )
    if mismatches:
        preview = "; ".join(mismatches[:20])
        remainder = len(mismatches) - 20
        suffix = f"; and {remainder} more" if remainder > 0 else ""
        raise locator_resolver.tool_error(
            context,
            f"Table column contains unexpected values: {preview}{suffix}",
            assertion_failed=True,
        )

    LOGGER.info(
        "Table column value check completed | pages=%s | cells=%s | column=%s | expected=%s",
        page_number,
        total_checked_cells,
        column_name,
        expected_value,
    )


async def _inspect_table_column_values(
    table: Any,
    column_index: int,
    column_name: str,
    expected_value: str,
    page_number: int,
) -> tuple[int, list[str]]:
    # Check one column on one pagination page and collect every mismatch.
    rows = table.locator("[role='row'], tbody tr")
    checked_cells = 0
    mismatches: list[str] = []
    for row_index in range(await rows.count()):
        row = rows.nth(row_index)
        if not await row.is_visible() or await row.locator("[role='columnheader'], th").count():
            continue
        cells = row.locator("[role='gridcell'], [role='cell'], td")
        cell_count = await cells.count()
        if cell_count == 0:
            continue

        row_id = await row.get_attribute("data-id") or await row.get_attribute("aria-rowindex") or str(row_index + 1)
        if column_index >= cell_count:
            mismatches.append(
                f"page={page_number}, row={row_id}, column={column_name}, reason=cell missing"
            )
            continue

        checked_cells += 1
        cell = cells.nth(column_index)
        actual_value = (await cell.inner_text()).strip()
        if not actual_value:
            actual_value = (await cell.get_attribute("aria-label") or "").strip()
        if expected_value.casefold() not in actual_value.casefold():
            mismatches.append(
                f'page={page_number}, row={row_id}, column={column_name}, '
                f'expected to contain="{expected_value}", actual="{actual_value}"'
            )
    return checked_cells, mismatches


async def _assert_table_columns_populated(page: Any, context: StepContext) -> None:
    # Read the required column names from the assertion step.
    required_columns = parsing.extract_quoted_values(context.step.text)
    if not required_columns:
        raise locator_resolver.tool_error(
            context,
            "Table population check requires at least one quoted column name.",
            assertion_failed=True,
        )

    # Use the first visible ARIA grid, ARIA table, or HTML table.
    table, table_index = await _first_visible_table(page)
    if table is None:
        raise locator_resolver.tool_error(
            context,
            "No visible table or grid was found on the current page.",
            ["[role='grid']", "[role='table']", "table"],
            assertion_failed=True,
        )

    # Read the actual headers and log which table was selected.
    headers = await _read_table_headers(table)
    # headers = [
    # {"name": "TailNumber"},
    # {"name": "Flight Number"},
    # {"name": "Departure"},
    # {"name": "Destination"},
    # ]
    table_details = await table.evaluate(
        """element => ({
            role: element.getAttribute('role') || element.tagName.toLowerCase(),
            id: element.id || '',
            className: typeof element.className === 'string' ? element.className : '',
            ariaRowCount: element.getAttribute('aria-rowcount') || ''
        })"""
    )
    LOGGER.info(
        "Table population check selected first visible table | index=%s | role=%s | id=%s | "
        "class=%s | aria_rowcount=%s | headers=%s",
        table_index,
        table_details["role"],
        table_details["id"] or "<none>",
        table_details["className"] or "<none>",
        table_details["ariaRowCount"] or "<none>",
        [header["name"] for header in headers],
    )

    page_number = 1
    total_checked_rows = 0
    empty_cells: list[str] = []
    # Store each page's row content so pagination cannot inspect the same page forever.
    visited_signatures: set[str] = set()

    while True:
        # Read headers again because some grids replace their DOM during pagination.
        headers = await _read_table_headers(table)
        header_lookup = {_table_key(header["name"]): index for index, header in enumerate(headers)}
        missing_columns = [
            column for column in required_columns if _table_key(column) not in header_lookup
        ]
        if missing_columns:
            raise locator_resolver.tool_error(
                context,
                f"Table columns were not found: {missing_columns}. Available columns: "
                f"{[header['name'] for header in headers]}",
                assertion_failed=True,
            )

        # Build a stable snapshot of the current rows before checking or changing pages.
        page_signature = await _table_page_signature(table)
        if page_signature in visited_signatures:
            raise locator_resolver.tool_error(
                context,
                f"Table pagination returned to an already inspected page at page {page_number}.",
                assertion_failed=True,
            )
        visited_signatures.add(page_signature)

        # Validate all required cells on the current pagination page.
        checked_rows, page_empty_cells = await _inspect_table_page(
            table,
            required_columns,
            header_lookup,
            page_number,
        )
        total_checked_rows += checked_rows
        empty_cells.extend(page_empty_cells)
        LOGGER.info(
            "Table population check inspected page | page=%s | rows=%s | required_columns=%s",
            page_number,
            checked_rows,
            required_columns,
        )

        # Stop at the last page when no visible and enabled Next control is available.
        next_button = await _find_table_next_button(table)
        if next_button is None or not await next_button.is_visible() or not await next_button.is_enabled():
            break

        next_label = (
            await next_button.get_attribute("aria-label")
            or await next_button.get_attribute("title")
            or await next_button.get_attribute("data-testid")
            or "<unlabeled>"
        )
        LOGGER.info(
            "Table population check moving to next page | current_page=%s | control=%s",
            page_number,
            next_label,
        )
        await next_button.click()
        # Confirm that clicking Next actually replaced the current table rows.
        await _wait_for_table_page_change(page, table, page_signature, page_number, context)
        page_number += 1

    if total_checked_rows == 0:
        raise locator_resolver.tool_error(
            context,
            "The selected table has no visible data rows.",
            assertion_failed=True,
        )
    if empty_cells:
        preview = "; ".join(empty_cells[:20])
        remainder = len(empty_cells) - 20
        suffix = f"; and {remainder} more" if remainder > 0 else ""
        raise locator_resolver.tool_error(
            context,
            f"Table contains empty required cells: {preview}{suffix}",
            assertion_failed=True,
        )

    LOGGER.info(
        "Table population check completed | pages=%s | rows=%s",
        page_number,
        total_checked_rows,
    )


async def _inspect_table_page(
    table: Any,
    required_columns: list[str],
    header_lookup: dict[str, int],
    page_number: int,
) -> tuple[int, list[str]]:
    # Inspect one pagination page and return its checked row count and empty-cell errors.
    rows = table.locator("[role='row'], tbody tr")
    checked_rows = 0
    empty_cells: list[str] = []
    for row_index in range(await rows.count()):
        row = rows.nth(row_index)
        if not await row.is_visible():
            continue
        if await row.locator("[role='columnheader'], th").count():
            continue
        cells = row.locator("[role='gridcell'], [role='cell'], td")
        cell_count = await cells.count()
        if cell_count == 0:
            continue

        checked_rows += 1
        row_id = await row.get_attribute("data-id") or await row.get_attribute("aria-rowindex") or str(checked_rows)
        for column in required_columns:
            column_index = header_lookup[_table_key(column)]
            if column_index >= cell_count:
                empty_cells.append(
                    f"page={page_number}, row={row_id}, column={column}, reason=cell missing"
                )
                continue
            cell = cells.nth(column_index)
            value = (await cell.inner_text()).strip()
            if _is_empty_table_value(value):
                value = (await cell.get_attribute("aria-label") or "").strip()
            if _is_empty_table_value(value):
                empty_cells.append(f"page={page_number}, row={row_id}, column={column}, reason=empty")
    return checked_rows, empty_cells


async def _find_table_next_button(table: Any) -> Any | None:
    # Locate a generic Next control without using an application-specific locator.
    # Accessible labels are preferred, with common right-arrow icon metadata as fallback.
    selectors = (
        'button[rel="next"]',
        'button[aria-label*="next" i]',
        'button[title*="next" i]',
        'button[aria-label*="sonraki" i]',
        'button[title*="sonraki" i]',
        'button[aria-label*="ileri" i]',
        'button[title*="ileri" i]',
        'button:has(svg[data-testid*="KeyboardArrowRight"])',
        'button:has(svg[data-testid*="NavigateNext"])',
        'button:has(svg[data-testid*="ChevronRight"])',
    )
    # Pagination is often a sibling of the grid, so search the table and nearby parents.
    search_roots = [table]
    parent = table.locator("xpath=..")
    for _ in range(3):
        search_roots.append(parent)
        parent = parent.locator("xpath=..")

    for root in search_roots:
        for selector in selectors:
            candidates = root.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    return candidate
    return None


async def _table_page_signature(table: Any) -> str:
    # Combine visible row ids and text into a snapshot used to detect page changes
    # and prevent pagination loops.
    # The function returns a string signature representing the current visible rows of the table. It iterates through each row, checks if it is visible and not a header, and then appends its identifier and text content to the signature. This allows the calling function to detect if the table page has changed after a pagination action by comparing signatures before and after the action.
    rows = table.locator("[role='row'], tbody tr")
    parts: list[str] = []
    for index in range(await rows.count()):
        row = rows.nth(index)
        if not await row.is_visible() or await row.locator("[role='columnheader'], th").count():
            continue
        row_id = await row.get_attribute("data-id") or await row.get_attribute("aria-rowindex") or ""
        parts.append(f"{row_id}:{(await row.inner_text()).strip()}")
    return "\n".join(parts)


async def _wait_for_table_page_change(
    page: Any,
    table: Any,
    previous_signature: str,
    page_number: int,
    context: StepContext,
) -> None:
    # Poll briefly after clicking Next until the visible row snapshot changes.
    # A timeout means the control did not navigate to a different table page.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await page.wait_for_timeout(250)
        current_signature = await _table_page_signature(table)
        if current_signature and current_signature != previous_signature:
            return
    raise locator_resolver.tool_error(
        context,
        f"Next page button was clicked, but table data did not change after page {page_number}.",
        assertion_failed=True,
    )


async def _first_visible_table(page: Any) -> tuple[Any | None, int]:
    candidates = page.locator("[role='grid'], [role='table'], table")
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if await candidate.is_visible():
            return candidate, index
    return None, -1


async def _read_table_headers(table: Any) -> list[dict[str, str]]:
    # Find header cells in both ARIA-based grids and standard HTML tables.
    header_cells = table.locator("[role='columnheader'], thead th")
    # Keep the headers in their displayed column order. The table assertion uses
    # each list index to read the corresponding cell from every data row.
    headers: list[dict[str, str]] = []
    for index in range(await header_cells.count()):
        cell = header_cells.nth(index)
        # Prefer visible header text, then fall back to accessibility or field metadata.
        name = (await cell.inner_text()).strip()
        if not name:
            name = (await cell.get_attribute("aria-label") or "").strip()
        if not name:
            name = (await cell.get_attribute("data-field") or "").strip()
        # A dictionary leaves room for additional header metadata later, such as
        # data-field or aria-colindex, without changing the function's return shape.
        headers.append({"name": name})
    return headers


def _table_key(value: str) -> str:
    # Normalize header names for comparison by ignoring case, spaces, and punctuation.
    # Example: "Flight Number" and "flight-number" both become "flightnumber".
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_empty_table_value(value: str) -> bool:
    # Normalize whitespace and case before checking common UI placeholders for empty data.
    normalized = " ".join(value.casefold().split())
    return normalized in {"", "-", "–", "—", "n/a", "na", "null", "none"}


async def _get_text(page: Any, context: StepContext) -> None:
    # Read text for logging; this tool does not assert the value.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        value = await locator.first.inner_text(timeout=10_000)
        LOGGER.info("Static get_text result | step=%s | value=%s", context.step.number, value)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read text: {exc}", tried)


async def _get_attribute(page: Any, context: StepContext) -> None:
    # Read an attribute for logging; attribute name defaults to href.
    attr_name = _extract_attribute_name(context.step.text)
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        value = await locator.first.get_attribute(attr_name, timeout=10_000)
        LOGGER.info("Static get_attribute result | step=%s | %s=%s", context.step.number, attr_name, value)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read attribute: {exc}", tried)


async def _get_input_value(page: Any, context: StepContext) -> None:
    # Read an input value for logging.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        value = await locator.first.input_value(timeout=10_000)
        LOGGER.info("Static get_input_value result | step=%s | value=%s", context.step.number, value)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not read input value: {exc}", tried)


async def _count_elements(page: Any, context: StepContext) -> None: # e.g. "Count how many items have the text 'Sale' in them" or "How many elements match the selector '.product-item'?"
    # Count elements matching the resolved selector or text locator.
    locator, tried = await locator_resolver.resolve_locator(page, context)
    try:
        count = await locator.count()
        LOGGER.info("Static count_elements result | step=%s | count=%s", context.step.number, count)
    except Exception as exc:
        raise locator_resolver.tool_error(context, f"Could not count elements: {exc}", tried)


async def _accept_dialog(page: Any, context: StepContext) -> None:
    # Register a one-time handler for the next native browser dialog.
    async def _handler(dialog: Any) -> None:
        await dialog.accept()

    page.once("dialog", _handler)


async def _dismiss_dialog(page: Any, context: StepContext) -> None:
    # Register a one-time handler for the next native browser dialog.
    async def _handler(dialog: Any) -> None:
        await dialog.dismiss()

    page.once("dialog", _handler)


async def _take_screenshot_step(page: Any, context: StepContext) -> None:
    # Store an explicit screenshot requested by the scenario.
    await _take_screenshot(page, context.test_key, f"step_{context.step.number}")


def _extract_key(text: str) -> str:
    keys = {
        "enter": "Enter",
        "tab": "Tab",
        "escape": "Escape",
        "esc": "Escape",
        "space": "Space",
        "backspace": "Backspace",
        "delete": "Delete",
    }
    lowered = text.lower()
    for token, key in keys.items():
        if token in lowered:
            return key
    quoted = parsing.extract_quoted_values(text)
    return quoted[0] if quoted else "Enter"


def _extract_file_path(text: str) -> str:
    quoted = parsing.extract_quoted_values(text)
    if quoted:
        return quoted[-1]
    match = re.search(r"([A-Za-z]:\\[^\s]+|/[^\s]+)", text)
    return match.group(1) if match else ""


def _extract_attribute_name(text: str) -> str:
    quoted = parsing.extract_quoted_values(text)
    if len(quoted) >= 2:
        return quoted[-1]
    match = re.search(r"\b(attribute|attr)\s+([a-zA-Z_:][-a-zA-Z0-9_:.]*)", text, re.IGNORECASE)
    return match.group(2) if match else "href"


def _extract_first_number(text: str, default: int = 1) -> int:
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else default


async def _take_screenshot(page: Any, test_key: str, label: str) -> str:
    # Screenshots are kept under logs/static_screenshots for pass/fail diagnostics.
    screenshot_dir = Config.PROJECT_ROOT / "logs" / "static_screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = screenshot_dir / f"{test_key}_{label}_{timestamp}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


async def _safe_error_screenshot(page: Any, test_key: str) -> str:
    if page is None:
        return ""
    try:
        return await _take_screenshot(page, test_key, "failed")
    except Exception:
        return ""


async def _safe_html_excerpt(page: Any) -> str:
    if page is None:
        return ""
    try:
        html = await page.content()
        return html[: Config.STATIC_HEALING_HTML_LIMIT]
    except Exception:
        return ""


def _relative_path(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).relative_to(Config.PROJECT_ROOT))
    except ValueError:
        return path
