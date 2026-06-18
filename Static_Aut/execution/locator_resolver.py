from typing import Any

from Static_Aut.execution.context import StepContext
from Static_Aut.execution.parsing import (
    extract_dropdown_target,
    extract_input_target,
    extract_role,
    extract_text_target,
    locator_key,
    looks_like_selector,
    slugify,
)
from Static_Aut.locators.locator_registry import locator_registry
from Static_Aut.profiles.app_profiles import app_profile_registry
from Static_Aut.toolbox.static_toolbox import StaticLocatorError


async def resolve_locator(
    page: Any,
    context: StepContext,
    fallback_selector: str = "",
) -> tuple[Any, list[str]]:
    # Resolve a Playwright locator for the current static tool step.
    # The returned tried list records every selector strategy attempted for reports/healing.
    step_text = context.step.text
    # First extract the most relevant target text based on the tool family.
    # Example input tool: 'Enter "john" into "Username" field' -> target='Username'.
    if context.tool_name in {"fill_input", "clear_input", "assert_input_value", "focus_element"}:
        target = extract_input_target(step_text) or extract_text_target(step_text)
    elif context.tool_name in {"select_option", "assert_dropdown_selected"}:
        target = extract_dropdown_target(step_text)
    else:
        target = extract_text_target(step_text)
    tried: list[str] = []
    candidates: list[tuple[str, Any]] = []

    # Prefer saved app-specific selectors first, including healed runtime selectors.
    saved_locator = await _resolve_saved_locator(page, context, target)
    if saved_locator:
        return saved_locator

    # If the step itself contains a raw selector, try it directly.
    if target and looks_like_selector(target):
        candidates.append((f"selector={target}", page.locator(target)))
    # Optional tool-specific fallback, such as input[type='file'] for uploads.
    if fallback_selector:
        candidates.append((f"selector={fallback_selector}", page.locator(fallback_selector)))
    if target:
        role = extract_role(step_text)
        # Dropdowns get select/option-specific strategies before generic locators.
        if context.tool_name in {"select_option", "assert_dropdown_selected"}:
            safe_target = _css_attr_substring(target)
            candidates.extend(
                [
                    (f"select:has(option:has-text({target}))", page.locator("select").filter(has_text=target)),
                    (f'option[label="{safe_target}"] >> xpath=ancestor::select[1]', page.locator(f'option[label="{safe_target}"]').locator("xpath=ancestor::select[1]")),
                    (f'option[value="{safe_target}"] >> xpath=ancestor::select[1]', page.locator(f'option[value="{safe_target}"]').locator("xpath=ancestor::select[1]")),
                ]
            )
        if role:
            candidates.append((f"role={role}, name={target}", page.get_by_role(role, name=target)))
        # Input-like controls often expose name/id/type attributes derived from the target.
        if context.tool_name in {"fill_input", "clear_input", "assert_input_value", "focus_element", "get_input_value"}:
            safe_target = _css_attr_substring(target)
            slug_target = slugify(target).replace("_", "-")
            candidates.extend(
                [
                    (f'input[name="{safe_target}"]', page.locator(f'input[name="{safe_target}"]')),
                    (f'input[id="{safe_target}"]', page.locator(f'input[id="{safe_target}"]')),
                    (f'textarea[name="{safe_target}"]', page.locator(f'textarea[name="{safe_target}"]')),
                    (f'textarea[id="{safe_target}"]', page.locator(f'textarea[id="{safe_target}"]')),
                    (f'input[name="{slug_target}"]', page.locator(f'input[name="{slug_target}"]')),
                    (f'input[id="{slug_target}"]', page.locator(f'input[id="{slug_target}"]')),
                    (f'input[type="{safe_target}"]', page.locator(f'input[type="{safe_target}"]')),
                ]
            )
        # Generic Playwright user-facing locators are tried after specific strategies.
        candidates.extend(
            [
                (f"label={target}", page.get_by_label(target)),
                (f"placeholder={target}", page.get_by_placeholder(target)),
                (f"text={target}", page.get_by_text(target, exact=False)),
                (f"title={target}", page.get_by_title(target)),
                (f"alt={target}", page.get_by_alt_text(target)),
            ]
        )
        if hasattr(page, "get_by_display_value"):
            candidates.append((f"display_value={target}", page.get_by_display_value(target)))

    # Check each candidate against the live page. The first locator with at least
    # one matching element wins.
    for description, locator in candidates:
        tried.append(description)
        try:
            if await locator.count() > 0:
                return locator, tried
        except Exception:
            # Invalid selector syntax or Playwright locator errors should not stop
            # fallback attempts; keep trying the next candidate.
            continue

    # Nothing matched the live page, so raise a structured error with all attempts.
    raise tool_error(context, "Could not resolve a target element from the step text.", tried)


def _css_attr_substring(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def tool_error(
    context: StepContext,
    message: str,
    tried_selectors: list[str] | None = None,
    assertion_failed: bool = False,
) -> StaticLocatorError:
    return StaticLocatorError(
        message=message,
        tool_name=context.tool_name,
        locator_key=locator_key(context.tool_name, context.step.text),
        tried_selectors=tried_selectors or [],
        assertion_failed=assertion_failed,
    )


async def _resolve_saved_locator(page: Any, context: StepContext, target: str) -> tuple[Any, list[str]] | None:
    # Try locators that are already registered for this app before generic fallbacks.
    app_name = context.app_name or app_profile_registry.default().app_name
    key = locator_key(context.tool_name, context.step.text)
    # context.tool_name = "click_element"
    # context.step.text = 'Click "Login" button'
    # key = "button__login"

    # First try the canonical locator key generated from the tool and step text.
    # Example: tool='click_element', step='Click "Login" button' -> key='button__login'.
    selector_groups: list[tuple[str, list[str]]] = [
        (key, locator_registry.get(app_name, key)),
    ]
    #     selector_groups = 
    #     [
    #     (
    #         "button__login",
    #         ["#login-button", "button[type='submit']"]
    #     )
    # ]

    tried: list[str] = []
    for key, selectors in selector_groups:
        for selector in selectors:
            # Record each saved selector attempt for failure reports and MCP healing.
            description = f"saved[{app_name}:{key}]={selector}"
            tried.append(description)
            locator = page.locator(selector)
            try:
                # A saved selector is accepted only if it matches something on the live page.
                if await locator.count() > 0:
                    return locator, tried
            except Exception:
                # Invalid or stale saved selectors should not block the next saved selector.
                continue
    # No saved selector matched the live page.
    return None
