import json
import asyncio
import re
from dataclasses import dataclass

from autogen_agentchat.conditions import TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat

from Agents.agentFactory import AgentFactory
from Jira_Aut.jira_bug_client import JiraBugClient
from LLMs.modelClient import ModelClient
from Resources.config import Config
from Resources.report_utils import TokenUsageSummary, calculate_token_usage, extract_last_text_message
from Static_Aut.execution import parsing
from Static_Aut.profiles.app_profiles import app_profile_registry
from Static_Aut.locators.locator_registry import locator_registry
from Static_Aut.toolbox.static_toolbox import StaticExecutionResult


HEALING_COMPLETED = "LOCATOR_HEALING_COMPLETED"
HEALING_START = "BEGIN_LOCATOR_HEALING"
HEALING_END = "END_LOCATOR_HEALING"


@dataclass(frozen=True)
class LocatorHealingPatch:
    app_name: str
    locator_key: str
    selector: str
    reason: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "LocatorHealingPatch":
        return cls(
            app_name=str(payload.get("app_name", "")).strip(),
            locator_key=str(payload.get("locator_key", "")).strip(),
            selector=str(payload.get("selector", "")).strip(),
            reason=str(payload.get("reason", "")).strip(),
        )

    def validate(
        self,
        expected_app_name: str,
        expected_locator_key: str,
        expected_target_text: str = "",
    ) -> None:
        # The patch must belong to the same application that produced the failure.
        if self.app_name != expected_app_name:
            raise ValueError(f"Unexpected app_name: {self.app_name!r}")
        # The MCP repair must update the exact locator key that failed, not a different one.
        if self.locator_key != expected_locator_key:
            raise ValueError(f"Unexpected locator_key: {self.locator_key!r}")
        # Empty selectors cannot be applied or persisted.
        if not self.selector:
            raise ValueError("Healing selector cannot be empty")
        # If the selector embeds visible text, ensure it still targets the failed text.
        _validate_selector_text_preserves_target(self.selector, expected_target_text)


@dataclass(frozen=True)
class LocatorHealingResult:
    patch: LocatorHealingPatch
    token_usage: TokenUsageSummary


async def heal_static_locator_with_mcp(
    scenario_text: str,
    static_result: StaticExecutionResult,
    app_name: str = "",
) -> LocatorHealingResult | None:
    if not static_result.failed_locator_key:
        return None
    app_name = app_name or static_result.app_name or app_profile_registry.default().app_name

    # Read the currently registered selectors for this failed locator key.
    # This does not query the live page; it gives the MCP repair agent context
    # about which selectors were known before healing.
    # Example current_selectors: ['#login-button', 'button[type="submit"]'].
    current_selectors = locator_registry.get(app_name, static_result.failed_locator_key)
    ############################################################################################
    target_text = parsing.locator_target(static_result.failed_tool, _failed_step_text(static_result))
    model_client = ModelClient.chat_model_client()
    factory = AgentFactory(model_client)
    locator_agent = factory.create_playwright_agent(
        system_message=(
            "You are a Playwright MCP locator repair agent.\n"
            "Your only job is to inspect the live page and return one improved locator "
            "for the failed static executor locator key.\n"
            "Do not execute the test end-to-end. Do not continue scenario steps after locating the element.\n"
            "If the target element is not visible yet, navigate and scroll just enough to reveal it.\n"
            "You must repair only locators, not test data. If the failed target text/name is not present exactly, "
            "do not infer, autocorrect, or choose a similar element.\n"
            "Do not return broad fallback selectors such as input[type='text'] to satisfy a misspelled target.\n"
            "Return exactly one JSON object between "
            f"{HEALING_START} and {HEALING_END}.\n"
            "JSON keys: app_name, locator_key, selector, reason.\n"
            "Do not wrap JSON in markdown fences.\n"
            f"After the JSON block, write exactly: {HEALING_COMPLETED}."
        )
    )
    healing_team = RoundRobinGroupChat(
        participants=[locator_agent],
        termination_condition=TextMentionTermination(HEALING_COMPLETED),
    )

    task = (
        "Repair the failed locator only.\n\n"
        f"App name: {app_name}\n"
        f"Failed tool: {static_result.failed_tool}\n"
        f"Failed locator key: {static_result.failed_locator_key}\n"
        f"Failed target text: {target_text}\n"
        f"Current selectors: {json.dumps(current_selectors, ensure_ascii=False)}\n"
        f"Tried selectors: {json.dumps(static_result.tried_selectors, ensure_ascii=False)}\n"
        f"Page URL at failure: {static_result.page_url}\n"
        f"Static error: {_truncate(static_result.error_message, 1000)}\n\n"
        "Scope rules:\n"
            "- Do not silently correct typos or replace the failed target text with a different visible label.\n"
            "- If the failed target text appears to be wrong, return a selector only if it still targets that exact text or stable non-text attributes for that exact element.\n"
            "- If the exact failed target cannot be found, return an empty selector and explain that the test target is absent.\n"
        "- Prefer stable CSS, role, text, or Playwright selectors. Never use ref-based selectors.\n\n"
        f"Scenario context:\n{scenario_text}\n\n"
        "HTML excerpt from failed static run:\n"
        f"{static_result.html_excerpt[:Config.STATIC_HEALING_HTML_LIMIT]}"
    )

    task_result = await _run_with_rate_limit_retry(healing_team, task)

    raw_text = extract_last_text_message(task_result)
    patch = LocatorHealingPatch.from_dict(_extract_healing_payload(raw_text))
    #   patch = 
    #     LocatorHealingPatch(
    #     app_name="herokuapp",
    #     locator_key="button__login",
    #     selector="#login",
    #     reason=""
    # )

    patch.validate(app_name, static_result.failed_locator_key, target_text)
    return LocatorHealingResult(
        patch=patch,
        token_usage=calculate_token_usage(task_result),
    )


async def _run_with_rate_limit_retry(healing_team, task: str):
    # Always allow at least one attempt, even if the config is set to 0.
    max_retries = max(1, Config.STATIC_HEALING_MAX_RETRIES)
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            # Successful execution returns immediately; no further retries are needed.
            return await healing_team.run(task=task)
        except Exception as exc:
            last_error = exc
            message = str(exc)
            # Only retry rate-limit failures. Other errors usually need real handling.
            if "RateLimitError" not in message and "rate_limit" not in message.lower():
                raise
            # If this was the final allowed attempt, re-raise the last error below.
            if attempt >= max_retries:
                break
            # Wait before retrying so the provider has time to clear the rate limit.
            await asyncio.sleep(_retry_delay_seconds(message, attempt))
    raise last_error


def _retry_delay_seconds(message: str, attempt: int) -> float:
    # Prefer the provider's explicit retry hint when the rate-limit message includes one.
    # Example message: "try again in 4.5s" -> wait 5.0 seconds.
    match = re.search(r"try again in ([0-9.]+)s", message, re.IGNORECASE)
    if match:
        # Add a small buffer and never retry in less than one second.
        return max(1.0, float(match.group(1)) + 0.5)
    # Fallback to exponential backoff: 2, 4, 8, 16, then max 20 seconds.
    return float(min(20, 2 ** attempt))


def _failed_step_text(static_result: StaticExecutionResult) -> str:
    # Best-effort lookup of the failed step text from the structured report.
    # Example: report.failed_step_text='Click "Login" button' -> returns that text.
    report_text = getattr(static_result, "report_text", "") or ""
    try:
        report = JiraBugClient.parse_automation_report(report_text)
        if report.failed_step_text:
            return report.failed_step_text
    except Exception:
        # Healing can still continue without step text; target extraction will be less specific.
        pass
    return ""


def _validate_selector_text_preserves_target(selector: str, expected_target_text: str) -> None:
    # Guardrail: if the healed selector contains visible text, it must keep
    # targeting the same text that originally failed.
    target = (expected_target_text or "").strip()
    if not target:
        # No expected target text means there is nothing text-specific to verify.
        return

    # Extract text embedded inside selectors like text="Login" or :has-text("Login").
    selector_texts = _selector_embedded_texts(selector)
    if not selector_texts:
        # Selectors based on ids/classes/roles do not embed visible text, so allow them.
        return

    # Normalize both sides so harmless casing/spacing differences do not fail the patch.
    # Example: " Login " and "login" compare as equal.
    normalized_target = _normalize_text_for_comparison(target)
    for selector_text in selector_texts:
        # Each embedded selector text must still point to the original failed target.
        # Example allowed: target="Login", selector_text="login".
        # Example rejected: target="Login", selector_text="Sign in".
        normalized_selector_text = _normalize_text_for_comparison(selector_text)
        if normalized_selector_text != normalized_target:
            # Reject patches that silently point to a different label/text.
            raise ValueError(
                "Healed selector appears to target different visible text "
                f"{selector_text!r} instead of failed target {target!r}"
            )


def _selector_embedded_texts(selector: str) -> list[str]:
    patterns = [
        r":has-text\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r":text\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"text\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?:^|[\s,])text\s*=\s*([^,\]\)\n]+)",
        r"role\s*=\s*[^,\]\)\n]+,\s*name\s*=\s*['\"]([^'\"]+)['\"]",
        r"role\s*=\s*[^,\]\)\n]+,\s*name\s*=\s*([^,\]\)\n]+)",
        r"get_by_text\(\s*['\"]([^'\"]+)['\"]",
        r"get_by_role\([^)]*name\s*=\s*['\"]([^'\"]+)['\"]",
        r"has_text\s*=\s*['\"]([^'\"]+)['\"]",
    ]
    texts: list[str] = []
    for pattern in patterns:
        for value in re.findall(pattern, selector, flags=re.IGNORECASE):
            if isinstance(value, tuple):
                value = next((item for item in value if item), "")
            value = str(value).strip(" '\"")
            if value:
                texts.append(value)
    return texts


def _normalize_text_for_comparison(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"


def apply_locator_patch(patch: LocatorHealingPatch) -> None:
    locator_registry.save_healed_selector(
        app_name=patch.app_name,
        locator_key=patch.locator_key,
        selector=patch.selector,
    )


def apply_runtime_locator_patch(patch: LocatorHealingPatch) -> None:
    locator_registry.apply_runtime_selector(
        app_name=patch.app_name,
        locator_key=patch.locator_key,
        selector=patch.selector,
    )


def _extract_healing_payload(raw_text: str) -> dict:
    # Preferred format: the agent wraps the JSON patch between explicit markers.
    # Example mcp response:
    #   BEGIN_LOCATOR_HEALING
    #   {"app_name": "herokuapp", "locator_key": "button__login", "selector": "#login"}
    #   END_LOCATOR_HEALING
    if HEALING_START in raw_text and HEALING_END in raw_text:
        json_block = raw_text.split(HEALING_START, 1)[1].split(HEALING_END, 1)[0]
        return json.loads(json_block.strip())

    # Fallback: if the markers are missing, try to decode the first JSON object
    # found in the text. This keeps healing tolerant of small formatting mistakes.
    first_brace = raw_text.find("{")
    if first_brace < 0:
        raise ValueError("Locator healing JSON payload was not found in MCP output")

    payload, _ = json.JSONDecoder().raw_decode(raw_text[first_brace:].strip())
    if not isinstance(payload, dict):
        raise ValueError("Locator healing payload must be a JSON object")
    # Return the parsed patch dict for LocatorHealingPatch.from_dict(...).
    return payload
    # {
    #     "app_name": "herokuapp",
    #     "locator_key": "button__login",
    #     "selector": "#login"
    # }
