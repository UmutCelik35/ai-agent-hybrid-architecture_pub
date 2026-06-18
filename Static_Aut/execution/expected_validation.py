import re

from Static_Aut.execution.parsing import (
    extract_dropdown_target,
    extract_input_target,
    extract_quoted_values,
    extract_url,
    extract_value,
    normalize_step_text,
)
from Static_Aut.toolbox.static_toolbox import ScenarioStep, resolve_web_tool_names


def build_expected_validation_step(
    action_step: ScenarioStep,
    expected_text: str,
    input_context_step: ScenarioStep | None = None,
    dropdown_context_step: ScenarioStep | None = None,
) -> tuple[ScenarioStep | None, str]:
    # Main Expected Result dispatcher.
    # It converts human-written expected text into a canonical ScenarioStep plus
    # the static assertion tool that should execute it.
    #
    # Example:
    #   action_step:  ScenarioStep(2, 'Click "Login" button')
    #   expected_text: '"Welcome" is visible'
    # returns:
    #   ScenarioStep(2, 'Verify text "Welcome" is visible'), "assert_text_visible"
    normalized = normalize_step_text(expected_text)
    if not normalized:
        return None, ""

    table_assertion = _build_table_column_contains_value_assertion(action_step, normalized)
    if table_assertion:
        return table_assertion

    table_assertion = _build_table_columns_populated_assertion(action_step, normalized)
    if table_assertion:
        return table_assertion

    # Try the most structured assertions first. Input value checks need special
    # handling because they can contain both a field name and an expected value.
    # Example: '"Username" field contains "john"'.
    inferred_input_assertion = _build_input_value_assertion(
        input_context_step or action_step,
        normalized,
    )
    
    if inferred_input_assertion:
        return inferred_input_assertion

    # Dropdown selected-value checks also need action context when the Expected
    # Result only names the option.
    # Example:
    #   Action: 'Select "Turkey" from "Country" dropdown'
    #   Expected Result: '"Turkey" is selected'
    dropdown_assertion = _build_dropdown_selected_assertion(
        dropdown_context_step or action_step,
        normalized,
    )
    if dropdown_assertion:
        return dropdown_assertion

    # Checkbox/radio expected states have their own parser because checked,
    # unchecked, selected, and unselected have different assertion tools.
    checkbox_assertion = _build_checkbox_state_assertion(action_step, normalized)
    if checkbox_assertion:
        return checkbox_assertion

    # First try matching the Expected Result as written, then try an assertion-style
    # variant. Some toolbox patterns expect words like "Verify", "Assert", or "Check".
    # Example: '"Login" button is visible' may not match, but
    # 'Verify that "Login" button is visible' can match assert_element_visible.
    for candidate_text in (normalized, f"Verify that {normalized}"):
        tool_name = _pick_assertion_tool(candidate_text)
        if tool_name:
            return ScenarioStep(number=action_step.number, text=candidate_text), tool_name

    # If direct tool matching did not work, inspect the text for generic visibility
    # hints and build a canonical assertion manually.
    lowered = normalized.lower()
    quoted_values = extract_quoted_values(normalized)
    has_negative_visibility = bool(
        re.search(r"\b(not visible|not displayed|hidden|absent|disappear|disappears|removed)\b", lowered)
    )
    has_element_hint = bool(
        re.search(r"\b(button|link|field|input|textbox|textarea|dropdown|checkbox|radio|element)\b", lowered)
    )
    has_image_hint = bool(
        re.search(r"\b(logo|image|icon|picture|grafik|gÃƒÂ¶rsel|resim|gorsel)\b", lowered)
    )

    # verify the page title when the expected result mentions "title" or its common synonyms in Turkish.
    if re.search(r"\b(title|baÃ…Å¸lÃ„Â±k|baslik)\b", lowered):
        expected_value = quoted_values[-1].strip() if quoted_values else extract_value(normalized)
        if expected_value:
            return (
                ScenarioStep(action_step.number, f'Verify title contains "{expected_value}"'),
                "assert_title_contains",
            )

    visibility_assertion = _build_text_visibility_assertion(action_step, normalized)
    if visibility_assertion:
        return visibility_assertion

    expected_url = extract_url(normalized)
    if expected_url:
        return (
            ScenarioStep(action_step.number, f'Verify URL contains "{expected_url}"'),
            "assert_url_contains",
        )

    # State-based assertions need an explicit target type. Without a dropdown,
    # checkbox, radio, etc., text like '"Turkey" is selected' is ambiguous and
    # should be fixed in Xray instead of being guessed as a visibility check.
    if re.search(r"\b(selected|selects|chosen|checked|unchecked)\b", lowered):
        return None, ""

    if quoted_values:
        target = quoted_values[0].strip()
        if has_image_hint and not has_negative_visibility:
            return (
                ScenarioStep(action_step.number, f'Verify image "{target}" is visible'),
                "assert_image_visible",
            )
        if has_image_hint and has_negative_visibility:
            return (
                ScenarioStep(action_step.number, f'Verify image "{target}" is not visible'),
                "assert_image_not_visible",
            )
        if has_element_hint:
            visibility_text = "not visible" if has_negative_visibility else "visible"
            return (
                ScenarioStep(action_step.number, f'Verify element "{target}" is {visibility_text}'),
                "assert_element_hidden" if has_negative_visibility else "assert_element_visible",
            )
        visibility_text = "not visible" if has_negative_visibility else "visible"
        return (
            ScenarioStep(action_step.number, f'Verify text "{target}" is {visibility_text}'),
            "assert_text_not_visible" if has_negative_visibility else "assert_text_visible",
        )

    return None, ""


def expand_expected_result_items(expected_text: str) -> list[str]: # the function takes an expected result text and tries to expand it into multiple assertion items if it contains multiple quoted targets. For example, if the expected text is '"Login" and "Register" buttons are visible', it will return ['"Login" button is visible', '"Register" button is visible'] by extracting the quoted values and applying the appropriate suffix based on the presence of keywords in the sentence. This allows the execution engine to check each target separately while still understanding that they were originally part of the same expected result statement.
    # Normalize quotes and whitespace before trying to split the expected result.
    normalized = normalize_step_text(expected_text)
    if not normalized:
        return []

    # A single quoted value is already one assertion target, so keep the text intact.
    quoted_values = extract_quoted_values(normalized)
    if len(quoted_values) <= 1:
        return [normalized]

    # Input-value assertions often contain two quoted values, such as
    # '"Username" field contains "john"'. Those must stay as one assertion.
    lowered = normalized.lower()
    if _looks_like_table_column_value_expected(lowered):
        return [normalized]
    if _looks_like_input_value_expected(lowered):
        return [normalized]

    # Multi-target visibility checks can be split into one assertion per quoted target.
    suffix = _multi_target_suffix(lowered)
    if not suffix:
        return [normalized]

    return [f'"{value}" {suffix}' for value in quoted_values] # such as '"Login" and "Register" buttons are visible' -> returns ['"Login" button is visible', '"Register" button is visible']


def _looks_like_input_value_expected(lowered_expected: str) -> bool:
    # Detect field/value assertions before expand_expected_result_items tries to split
    # multiple quoted values into separate assertions.
    #
    # Example that must stay as one assertion:
    #   '"Username" field contains "john"'
    #
    # It has two quoted values, but they are not two independent targets:
    #   - "Username" is the input-like control.
    #   - "john" is the expected value inside that control.
    #
    # Without this guard, the generic multi-target splitter could incorrectly treat
    # both quoted values as separate visible targets.
    
    return bool(
        re.search(r"\b(input|text\s*box|textbox|textarea|field|alan)\b", lowered_expected)
        and re.search(
            r"\b(value|equals|equal|contains|contain|entered|typed|filled|girdi|deger)\b",
            lowered_expected,
        )
    )


def _looks_like_table_column_value_expected(lowered_expected: str) -> bool:
    # Detect a table column/value assertion before the generic multi-target splitter
    # treats the quoted column name and expected value as two separate assertions.
    # lowered_expected is the full Expected Result converted to lowercase so the
    # regular expressions can match keywords without case-sensitive variations.
    return bool(
        re.search(
            r"\b(table|grid|tablo|tablodaki|tablonun|columns?|kolon\w*|sütun\w*|sutun\w*)\b",
            lowered_expected,
        )
        and re.search(r"\b(cells?|hücre\w*|hucre\w*)\b", lowered_expected)
        and re.search(
            r"\b(contain|contains|include|includes|içer|icer|içermeli|icermeli|içermelidir|icermelidir)\b",
            lowered_expected,
        )
    )


def _build_table_column_contains_value_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    lowered = normalized_expected.lower()
    has_column = bool(re.search(r"\b(columns?|kolon\w*|sütun\w*|sutun\w*)\b", lowered))
    has_cells = bool(re.search(r"\b(cells?|hücre\w*|hucre\w*)\b", lowered))
    has_all = bool(re.search(r"\b(all|every|each|tüm|tum|her)\b", lowered))
    has_contains = bool(
        re.search(
            r"\b(contain|contains|include|includes|içer|icer|içermeli|icermeli|içermelidir|icermelidir)\b",
            lowered,
        )
    )
    quoted_values = [value.strip() for value in extract_quoted_values(normalized_expected) if value.strip()]
    if not (has_column and has_cells and has_all and has_contains and len(quoted_values) >= 2):
        return None

    column_name = quoted_values[0]
    expected_value = quoted_values[-1]
    return (
        ScenarioStep(
            action_step.number,
            f'Verify every cell in table column "{column_name}" contains "{expected_value}"',
        ),
        "assert_table_column_contains_value",
    )


def _build_table_columns_populated_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    lowered = normalized_expected.lower()
    has_table = bool(re.search(r"\b(table|grid|tablo|tablonun|tablodaki)\b", lowered))
    has_rows = bool(
        re.search(
            r"\b(every|each|all|her|tüm|tum)\b.*\b(rows?|satır(?:da|daki|larda)?|satir(?:da|daki|larda)?)\b",
            lowered,
        )
    )
    has_columns = bool(
        re.search(r"\b(columns?|kolon(?:lar|ları|lari)?|sütun(?:lar|ları)?|sutun(?:lar|lari)?)\b", lowered)
    )
    has_populated = bool(
        re.search(
            r"\b(populated|filled|non[- ]?empty|not empty|dolu|boş olmamalı|bos olmamali)\b",
            lowered,
        )
    )
    columns = [value.strip() for value in extract_quoted_values(normalized_expected) if value.strip()]
    if not (has_table and has_rows and has_columns and has_populated and columns):
        return None

    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    return (
        ScenarioStep(
            action_step.number,
            f"Verify every row in the first visible table has populated columns {quoted_columns}",
        ),
        "assert_table_columns_populated",
    )


def _multi_target_suffix(lowered_expected: str) -> str:
    # Build the reusable assertion suffix for expected results that mention multiple
    # quoted UI targets in one sentence.
    #
    # Example input:
    #   '"Login" and "Register" buttons are visible'
    #
    # expand_expected_result_items extracts the quoted values ["Login", "Register"].
    # This helper returns 'button is visible', so the caller can produce:
    #   '"Login" button is visible'
    #   '"Register" button is visible'
    #
    # The same idea works for negative checks:
    #   '"Spinner" and "Loader" are not visible'
    # becomes:
    #   '"Spinner" is not visible'
    #   '"Loader" is not visible'
    has_negative_visibility = bool(
        re.search(r"\b(not visible|not displayed|hidden|absent|disappear|disappears|removed)\b", lowered_expected)
    )
    visibility_text = "not visible" if has_negative_visibility else "visible"
    # Prefer a typed suffix when the sentence tells us what kind of UI target it is.
    if re.search(r"\b(buttons?|links?|fields?|inputs?|elements?)\b", lowered_expected):
        return f"button is {visibility_text}"
    if re.search(r"\b(texts?|messages?|labels?)\b", lowered_expected):
        return f"text is {visibility_text}"
    if re.search(r"\b(images?|logos?|icons?|pictures?)\b", lowered_expected):
        return f"image is {visibility_text}"
    # If the sentence only says visible/hidden without a target type, keep a generic suffix.
    if re.search(r"\b(visible|displayed|shown|not visible|not displayed|hidden|absent)\b", lowered_expected):
        return f"is {visibility_text}"
    return ""


def _build_checkbox_state_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    lowered = normalized_expected.lower()
    if not re.search(r"\b(checkbox|radio)\b", lowered):
        return None

    unchecked_intent = bool(
        re.search(
            r"\b(unchecked|not checked|not be checked|unselected|not selected|not be selected|"
            r"unticked|not ticked|disabled|clear|cleared)\b",
            lowered,
        )
    )
    checked_intent = bool(
        re.search(r"\b(checked|selected|ticked|enabled)\b", lowered)
    )
    if not (checked_intent or unchecked_intent):
        return None

    quoted_values = extract_quoted_values(normalized_expected)
    target = quoted_values[0].strip() if quoted_values else _extract_checkbox_target(normalized_expected)
    if not target:
        return None

    state_text = "unchecked" if unchecked_intent else "checked"
    tool_name = "assert_checkbox_unchecked" if unchecked_intent else "assert_checkbox_checked"
    return ScenarioStep(action_step.number, f'Verify checkbox "{target}" is {state_text}'), tool_name


def _extract_checkbox_target(text: str) -> str:
    cleaned = re.sub(
        r"\b(verify|assert|check|ensure|that|the|a|an|field|input|is|are|should|must|be|"
        r"checked|selected|ticked|enabled|unchecked|not checked|not be checked|"
        r"unselected|not selected|not be selected|unticked|not ticked|disabled|"
        r"clear|cleared)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" .:-")


def _build_dropdown_selected_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    # Convert dropdown Expected Results into the canonical static assertion:
    #   Verify dropdown "<dropdown>" selected value is "<option>"
    #
    # Example with both dropdown and option in the Expected Result:
    #   Expected Result: '"Country" dropdown selected value is "Turkey"'
    # becomes:
    #   ScenarioStep(..., 'Verify dropdown "Country" selected value is "Turkey"'),
    #   "assert_dropdown_selected"
    lowered = normalized_expected.lower()
    normalized_action = normalize_step_text(action_step.text)
    lowered_action = normalized_action.lower()

    # Accept dropdown context from either the Expected Result itself or the action
    # that produced it.
    # Example:
    #   Action: 'Select "Turkey" from "Country" dropdown'
    #   Expected Result: '"Turkey" is selected'
    has_dropdown_context = bool(
        re.search(r"\b(dropdown|combo|select|list)\b", lowered)
        or re.search(r"\b(select|choose)\b.*\b(dropdown|combo|list|option)\b", lowered_action)
    )
    if not has_dropdown_context:
        return None

    # The expected text must actually describe a selected/chosen/value state.
    if not re.search(r"\b(selected|selects|chosen|value)\b", lowered):
        return None

    # Two quoted values usually mean: dropdown target + expected option.
    # One quoted value means: expected option comes from Expected Result,
    # dropdown target comes from the action step.
    quoted_values = extract_quoted_values(normalized_expected)
    if len(quoted_values) >= 2:
        dropdown_target = quoted_values[0].strip()
        expected_option = quoted_values[-1].strip()
    elif len(quoted_values) == 1 and lowered_action:
        dropdown_target = extract_dropdown_target(normalized_action)
        expected_option = quoted_values[0].strip()
    else:
        return None

    if dropdown_target and expected_option:
        return (
            ScenarioStep(
                action_step.number,
                f'Verify dropdown "{dropdown_target}" selected value is "{expected_option}"',
            ),
            "assert_dropdown_selected",
        )
    return None


def should_skip_expected_validation(expected_text: str) -> bool:
    # Decide whether an Expected Result should be ignored because it is procedural
    # text rather than a verifiable assertion.
    #
    # Example to skip:
    #   'User enters username and password'
    #
    # Example to keep:
    #   'Welcome message is visible'
    # True → validation skip 
    # False → validation does not skip  

    normalized = normalize_step_text(expected_text).lower()
    if not normalized:
        return True

    # These phrases usually describe actions that belong in the Action field,
    # not outcomes that can be asserted.
    procedural_only = [
        r"\buser enters\b",
        r"\buser types\b",
        r"\benter the\b",
        r"\btype the\b",
        r"\bfill the\b",
        r"\bclick the\b",
        r"\bnavigate to\b",
        r"\bkullanici girer\b",
        r"\bkullanÃ„Â±cÃ„Â± girer\b",
        r"\bkullanici yazar\b",
        r"\bkullanÃ„Â±cÃ„Â± yazar\b",
        r"\btiklar\b",
        r"\btÃ„Â±klar\b",
    ]
    has_procedural = any(re.search(pattern, normalized) for pattern in procedural_only)
    if not has_procedural:
        return False

    # If procedural text also contains assertion language, keep it.
    # Example: 'User enters username and the field contains "john"' is still
    # potentially verifiable because it includes 'contains'.
    has_assertive_markers = bool(
        re.search(
            r"\b(verify|assert|check|ensure|should|must|is visible|displayed|shown|contains|equals|doÃ„Å¸rula|kontrol)\b",
            normalized,
        )
    )

    # Skip only when it looks procedural and has no assertion marker.
    return not has_assertive_markers


def _build_text_visibility_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    # Convert plain text visibility Expected Results into canonical assertions:
    #   Verify text "<target>" is visible
    #   Verify text "<target>" is not visible
    #
    # Example:
    #   Expected Result: '"Welcome" is visible'
    # becomes:
    #   ScenarioStep(..., 'Verify text "Welcome" is visible'), "assert_text_visible"
    lowered = normalized_expected.lower()

    # Image/logo/icon checks are handled by image-specific assertion builders.
    # Example: '"Company Logo" image is visible' should not become a text assertion.
    if re.search(r"\b(logo|image|icon|picture|grafik|gorsel|resim)\b", lowered):
        return None

    # Element-specific checks are handled by element/input/dropdown/checkbox builders.
    # Example: '"Login" button is visible' should become an element assertion, not text.
    if re.search(r"\b(element|button|link|field|input|textbox|textarea|dropdown|checkbox|radio)\b", lowered):
        return None

    # Detect negative visibility first so text such as "not visible" does not get
    # treated as a normal visible assertion just because it also contains "visible".
    has_hidden_intent = bool(
        re.search(
            r"\b(not visible|not displayed|hidden|absent|disappear|disappears|removed|"
            r"gizli|yok|gorunmuyor)\b",
            lowered,
        )
    )

    # Detect positive text visibility phrases.
    has_visible_intent = bool(
        re.search(
            r"\b(visible|displayed|shown|appear|appears|gorunur|gorunuyor)\b",
            lowered,
        )
    )
    if not (has_visible_intent or has_hidden_intent):
        return None

    # Prefer quoted target text. If there is no quote, try to infer the target from
    # the remaining sentence.
    # Example: '"Welcome" is visible' -> 'Welcome'.
    quoted_values = extract_quoted_values(normalized_expected)
    target = quoted_values[0].strip() if quoted_values else _extract_visibility_target(normalized_expected)
    if not target:
        return None

    # Choose the matching assertion tool based on positive vs negative visibility.
    visibility_text = "not visible" if has_hidden_intent else "visible"
    tool_name = "assert_text_not_visible" if has_hidden_intent else "assert_text_visible"
    return ScenarioStep(action_step.number, f'Verify text "{target}" is {visibility_text}'), tool_name


def _extract_visibility_target(text: str) -> str:
    cleaned = re.sub(
        r"\b(verify|assert|check|ensure|see|that|the|a|an|text|message|label|"
        r"dogrula|kontrol)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(is|are|should|must|be|become|becomes|visible|displayed|shown|appear|appears|"
        r"not visible|not displayed|hidden|absent|disappear|disappears|removed|"
        r"gorunur|gorunuyor|gizli|yok|gorunmuyor)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" .:-")


def _build_input_value_assertion(
    action_step: ScenarioStep,
    normalized_expected: str,
) -> tuple[ScenarioStep, str] | None:
    # Convert input-value Expected Results into the canonical static assertion:
    #   Verify input "<field>" has value "<expected value>"
    #
    # Example with both field and value in the Expected Result:
    #   Expected Result: '"Username" field contains "john"'
    # becomes:
    #   ScenarioStep(..., 'Verify input "Username" has value "john"'), "assert_input_value"
    lowered = normalized_expected.lower()
    normalized_action = normalize_step_text(action_step.text)
    lowered_action = normalized_action.lower()

    # If the expected text itself describes a user typing/filling action, only treat it
    # as input validation when the original action step is also input-like.
    # This avoids converting unrelated expected prose into input assertions.
    if re.search(
        r"\b(user enters|user types|enter the|type the|fill the|kullanici girer|kullanici yazar)\b",
        lowered,
    ):
        if not _looks_like_input_action(lowered_action):
            return None

    # Detect whether the Expected Result explicitly references an input-like control.
    # Example: '"Email" field contains "a@b.com"'.
    has_input_intent = bool(
        re.search(
            r"\b(input|text\s*box|textbox|textarea|field|alan)\b",
            lowered,
        )
    )

    # If the Expected Result is terse, borrow the input intent from the action step.
    # Example:
    #   Action: 'Enter "john" into "Username" field'
    #   Expected Result: '"john"'
    if not has_input_intent and _looks_like_input_action(lowered_action):
        has_input_intent = True

    # Detect whether the Expected Result is about a value being entered, shown,
    # contained, or equal to something.
    has_value_intent = bool(
        re.search(
            r"\b(enter|enters|entered|type|typed|fill|filled|contains|contain|equals|equal|"
            r"shows|show|displays|display|yazar|girer|girdi|deger|value)\b",
            lowered,
        )
    )

    # A single quoted value after an input action is treated as the expected value.
    # Example:
    #   Action: 'Enter "john" into "Username" field'
    #   Expected Result: '"john"'
    if not has_value_intent and _looks_like_input_action(lowered_action) and len(extract_quoted_values(normalized_expected)) == 1:
        has_value_intent = True
    if not (has_input_intent and has_value_intent):
        return None

    # Two quoted values usually mean: first/identified field + expected value.
    # One quoted value means: expected value comes from Expected Result, field comes from action.
    quoted_values = extract_quoted_values(normalized_expected)
    if len(quoted_values) >= 2:
        field_name, expected_value = _input_field_and_value_from_quoted_expected(
            normalized_expected,
            quoted_values,
        )
    elif len(quoted_values) == 1:
        expected_value = quoted_values[0].strip()
        field_name = extract_input_target(normalized_action)
        if not field_name:
            return None
    else:
        return None

    if not field_name or not expected_value:
        return None
    step_text = f'Verify input "{field_name}" has value "{expected_value}"'
    return ScenarioStep(number=action_step.number, text=step_text), "assert_input_value"
    # the function returns such as ScenarioStep(number=2, text='Verify input "Username" has value "john"'), "assert_input_value"


def _input_field_and_value_from_quoted_expected(
    normalized_expected: str,
    quoted_values: list[str],
) -> tuple[str, str]:
    first = quoted_values[0].strip()
    last = quoted_values[-1].strip()
    escaped_first = re.escape(first)
    escaped_last = re.escape(last)
    value_into_field = re.search(
        rf'["\']{escaped_first}["\'].*\b(?:into|in|to)\b.*["\']{escaped_last}["\'].*\b(?:field|input|text\s*box|textbox|textarea)\b',
        normalized_expected,
        re.IGNORECASE,
    )
    if value_into_field:
        return last, first
    return quoted_values[-2].strip(), last


def _looks_like_input_action(text: str) -> bool:
    return bool(
        re.search(
            r"\b(fill|filled|enter|enters|entered|input|set|write|type|types|typed|send\s*keys?|sendkeys|yaz|doldur|gir|girer|girdi)\b",
            text,
        )
        and re.search(
            r"\b(field|input|text\s*box|textbox|textarea|alan)\b",
            text,
        )
    )


def _pick_assertion_tool(step_text: str) -> str:
    allowed_tools = {
        "assert_text_visible",
        "assert_text_not_visible",
        "assert_image_visible",
        "assert_image_not_visible",
        "assert_element_visible",
        "assert_element_hidden",
        "assert_checkbox_checked",
        "assert_checkbox_unchecked",
        "assert_dropdown_selected",
        "assert_url_contains",
        "assert_title_contains",
        "assert_input_value",
        "assert_table_column_contains_value",
        "assert_table_columns_populated",
        "wait_for_text",
        "wait_for_element",
    }
    for tool_name in resolve_web_tool_names(step_text):
        if tool_name in allowed_tools:
            return tool_name
    return ""
