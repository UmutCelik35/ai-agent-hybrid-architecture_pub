import re
from datetime import datetime


URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
QUOTED_TEXT_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'|`([^`]+)`') #captures text enclosed in double quotes, single quotes, or backticks, allowing for flexible extraction of quoted values from the step text.
DYNAMIC_TODAY_KEYWORDS = {"today", "bugun", "bugün"}
DEFAULT_DYNAMIC_DATE_FORMAT = "%d.%m.%Y"
QUOTE_REPLACEMENTS = ( # means to normalize various types of quotes and apostrophes to standard ASCII quotes for easier parsing. For example, it replaces curly quotes (“ ” ‘ ’) and some Unicode quote characters with straight quotes (" ').
    ("\u201c", '"'),
    ("\u201d", '"'),
    ("\u2018", "'"),
    ("\u2019", "'"),
    ("\u00e2\u20ac\u0153", '"'),
    ("\u00e2\u20ac\u009d", '"'),
    ("\u00e2\u20ac\u02dc", "'"),
    ("\u00e2\u20ac\u2122", "'"),
)


def normalize_quotes(text: str) -> str:
    # getting rid of various types of quotes and apostrophes to standard ASCII quotes for easier parsing. For example, it replaces curly quotes (“ ” ‘ ’) and some Unicode quote characters with straight quotes (" ').
    normalized = text or ""
    for source, replacement in QUOTE_REPLACEMENTS:
        normalized = normalized.replace(source, replacement)
    return normalized


def normalize_step_text(text: str) -> str:
    return " ".join(normalize_quotes(text).split()).strip()


def extract_url(text: str) -> str:
    # Find the first URL-like value in the text.
    # Example: 'Navigate to https://example.com/login.' -> 'https://example.com/login'.
    match = URL_RE.search(text)
    # Strip punctuation that often appears after a URL in prose or markdown.
    return match.group(0).rstrip(".,);]") if match else ""


def extract_quoted_values(text: str) -> list[str]:
    # This function extracts text enclosed in quotes (single, double, or backticks) from the input text. It first normalizes the quotes to standard ASCII quotes for consistent parsing, then uses a regular expression to find all quoted segments and returns them as a list of strings with leading/trailing whitespace removed.
    normalized_text = normalize_quotes(text)
    values: list[str] = []
    for match in QUOTED_TEXT_RE.finditer(normalized_text):
        values.append(next(group for group in match.groups() if group))
    return values #such as text = 'Select "Turkey" from "Country" dropdown' -> returns ["Turkey", "Country"]


def extract_text_target(text: str) -> str:
    # Prefer explicit quoted targets because they are the least ambiguous.
    # Example: 'Click "Login" button' -> 'Login'.
    quoted = extract_quoted_values(text)
    if quoted:
        return quoted[0].strip()

    # URLs are not useful as visible text targets, so remove them before guessing.
    cleaned = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)

    # Remove action/assertion verbs to leave behind the target-like words.
    # Example: 'Verify Login button is visible' -> 'Login button is visible'.
    cleaned = re.sub(
        r"\b(click|tap|press|select|double click|right click|hover|focus|wait for|find|search|"
        r"verify|assert|check|ensure|fill|enter|input|set|write|clear|type|scroll to|count|"
        r"tikla|tÃ„Â±kla|bas|sec|seÃƒÂ§|ara|bul|dogrula|doÃ„Å¸rula|kontrol|yaz|doldur|gir|bekle)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Remove generic UI words and filler words, keeping the specific label/name.
    # Example: 'Login button is visible' -> 'Login'.
    cleaned = re.sub(
        r"\b(the|a|an|button|link|field|input|textbox|textarea|element|text|message|label|"
        r"visible|displayed|shown|hidden|absent|not|is|are|should|see|"
        r"buton|alan|metin|yazi|yazÃ„Â±|mesaj|element|gorunur|gÃƒÂ¶rÃƒÂ¼nÃƒÂ¼r|gizli|yok|"
        r"gorunmuyor|gÃƒÂ¶rÃƒÂ¼nmÃƒÂ¼yor|ile|to|on|in|into|by|named|called)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Collapse extra spaces and trim punctuation left behind by the removals.
    return re.sub(r"\s+", " ", cleaned).strip(" .:-")


def extract_value(text: str) -> str:
    # Prefer quoted values because they usually identify the exact value to assert or type.
    # Example: 'Verify title contains "Dashboard"' -> 'Dashboard'.
    quoted = extract_quoted_values(text)
    if quoted:
        # Input actions often have two quoted values: value first, field second.
        # Example: 'Enter "john" into "Username" field' -> 'john'.
        if len(quoted) >= 2 and _looks_like_value_into_field(text, quoted[0], quoted[-1]):
            return quoted[0].strip()
        # For assertions, the expected value is usually the last quoted value.
        # Example: 'Verify input "Username" has value "john"' -> 'john'.
        return quoted[-1].strip()

    # If there are no quotes, use common value-introducing words.
    # Example: 'Set username field to john' -> 'john'.
    match = re.search(r"\b(?:with|as|to|value|deger|deÃ„Å¸er)\s+(.+)$", text, re.IGNORECASE)
    if match:
        return match.group(1).strip(" .")

    # Last fallback: try to extract a text target; if that fails, return the cleaned text.
    target = extract_text_target(text)
    return target or text.strip()


def resolve_runtime_input_value(value: str, now: datetime | None = None) -> str:
    # Allow scenarios to use stable placeholders such as "Today" for date inputs.
    cleaned = (value or "").strip()
    if cleaned.casefold() in DYNAMIC_TODAY_KEYWORDS:
        current = now.astimezone() if now else datetime.now().astimezone()
        return current.strftime(DEFAULT_DYNAMIC_DATE_FORMAT)
    return value


def extract_input_target(text: str) -> str:
    # Extract the field/control name from input-related step text.
    # Example: 'Enter "john" into "Username" field' -> 'Username'.
    normalized = normalize_step_text(text)
    quoted = extract_quoted_values(normalized)

    # With two or more quoted values, decide which quoted value is the field name.
    # Example action: 'Enter "john" into "Username" field' -> field is the last quote.
    # Example assertion: 'Verify input "Username" has value "john"' -> field is the previous quote.
    if len(quoted) >= 2 and re.search(r"\b(field|input|text\s*box|textbox|textarea)\b", normalized, re.IGNORECASE):
        if _looks_like_value_into_field(normalized, quoted[0], quoted[-1]):
            return quoted[-1].strip()
        return quoted[-2].strip()

    # With one quoted value and input wording, treat that quote as the field name.
    # Example: 'Clear "Username" field' -> 'Username'.
    if len(quoted) == 1 and re.search(r"\b(field|input|text\s*box|textbox|textarea)\b", normalized, re.IGNORECASE):
        return quoted[0].strip()

    # Fallback for unquoted phrases.

    # Examples:
    #   'Enter john into the username field' -> 'username'
    #   'field named username' -> 'username'
    patterns = [
        r"\b(?:in|into|to)\s+(?:the\s+)?(.+?)\s+(?:field|input|text\s*box|textbox|textarea)\b",
        r"\b(?:field|input|text\s*box|textbox|textarea)\s+(?:named|called)\s+(.+?)\b",
    ]
    # If there are no quotes, infer the input field name from phrases like
    # "into/in/to ... field/input/textbox" or "field named ...".
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip(" .:-")
        candidate = re.sub(r"^(?:the|a|an)\s+", "", candidate, flags=re.IGNORECASE)
        if candidate:
            return candidate
    # Return an empty string when the input target cannot be inferred safely.
    return ""


def _looks_like_value_into_field(text: str, value: str, field: str) -> bool:
    escaped_value = re.escape(value)
    escaped_field = re.escape(field)
    return bool(
        re.search(
            rf'["\']{escaped_value}["\'].*\b(?:into|in|to)\b.*["\']{escaped_field}["\'].*\b(?:field|input|text\s*box|textbox|textarea)\b',
            text,
            re.IGNORECASE,
        )
    )


def extract_dropdown_target(text: str) -> str:
    quoted = extract_quoted_values(text)
    if len(quoted) >= 2:
        lowered = normalize_step_text(text).lower()
        if re.search(r"\b(verify|assert|ensure|check)\b", lowered) and re.search(
            r"\b(selected|chosen|value)\b", lowered
        ):
            return quoted[0].strip()
        return quoted[-1].strip()
    if quoted:
        return quoted[0].strip()
    return ""


def extract_dropdown_option(text: str) -> str:
    quoted = extract_quoted_values(text)
    if len(quoted) >= 2:
        return quoted[0].strip()
    if quoted:
        return quoted[0].strip()

    normalized = normalize_step_text(text)
    match = re.search(
        r"\b(?:select|choose|sec|seÃ§)\s+(.+?)\s+\b(?:option|from|dropdown|combo|list)\b",
        normalized,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" .:-")
    return extract_value(text)


def extract_selector(text: str) -> str:
    match = re.search(r"\b(?:css|selector|xpath)\s*=\s*([^\s]+)", text, re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        return value if not value.lower().startswith("xpath") else value
    for value in extract_quoted_values(text):
        if looks_like_selector(value):
            return value
    return ""


def extract_role(text: str) -> str:
    lowered = text.lower()
    role_map = {
        "button": ("button", "buton"),
        "link": ("link",),
        "textbox": ("textbox", "input", "field", "alan"),
        "checkbox": ("checkbox",),
        "radio": ("radio",),
        "combobox": ("dropdown", "combo", "select"),
    }
    for role, tokens in role_map.items():
        if any(token in lowered for token in tokens):
            return role
    return ""


def looks_like_selector(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith(("css=", "xpath=", "//", "(", "#", ".", "[", "text=", "role=")):
        return True
    return bool(re.search(r"[>#.~:[\]=]", stripped))


def slugify(text: str, limit: int = 80) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:limit]


CHECKBOX_LOCATOR_TOOLS = {
    "check_checkbox",
    "uncheck_checkbox",
    "assert_checkbox_checked",
    "assert_checkbox_unchecked",
}
INPUT_LOCATOR_TOOLS = {
    "fill_input",
    "clear_input",
    "type_text",
    "focus_element",
    "assert_input_value",
    "get_input_value",
}
DROPDOWN_LOCATOR_TOOLS = {
    "select_option",
    "assert_dropdown_selected",
}
TEXT_LOCATOR_TOOLS = {
    "assert_text_visible",
    "assert_text_not_visible",
    "wait_for_text",
    "find_text",
}
IMAGE_LOCATOR_TOOLS = {
    "assert_image_visible",
    "assert_image_not_visible",
}
GENERIC_TARGET_LOCATOR_TOOLS = {
    "click_element",
    "double_click_element",
    "right_click_element",
    "hover_element",
    "wait_for_element",
    "assert_element_visible",
    "assert_element_hidden",
    "scroll_to_element",
    "get_text",
    "get_attribute",
    "count_elements",
}


def locator_key(tool_name: str, step_text: str) -> str:
    family = locator_family(tool_name, step_text)
    target = locator_target(tool_name, step_text)
    target_slug = slugify(target)
    if family and target_slug:
        return f"{family}__{target_slug}" # such as "button__login"
    return legacy_locator_key(tool_name, step_text)


def legacy_locator_key(tool_name: str, step_text: str) -> str:
    slug = slugify(step_text)
    return f"{tool_name}__{slug}" if slug else tool_name # such as "click_element__click_login_button_is_visible"


def locator_family(tool_name: str, step_text: str) -> str:
    if tool_name in CHECKBOX_LOCATOR_TOOLS:
        return "checkbox"
    if tool_name in INPUT_LOCATOR_TOOLS:
        return "input"
    if tool_name in DROPDOWN_LOCATOR_TOOLS:
        return "dropdown"
    if tool_name in TEXT_LOCATOR_TOOLS:
        return "text"
    if tool_name in IMAGE_LOCATOR_TOOLS:
        return "image"
    if tool_name in GENERIC_TARGET_LOCATOR_TOOLS:
        role = extract_role(step_text)
        if role in {"button", "link", "textbox"}:
            return "input" if role == "textbox" else role
        return "element"
    return ""


def locator_target(tool_name: str, step_text: str) -> str:
    if tool_name in INPUT_LOCATOR_TOOLS:
        return extract_input_target(step_text) or extract_text_target(step_text)
    if tool_name in DROPDOWN_LOCATOR_TOOLS:
        return extract_dropdown_target(step_text) or extract_text_target(step_text)
    return extract_text_target(step_text)
