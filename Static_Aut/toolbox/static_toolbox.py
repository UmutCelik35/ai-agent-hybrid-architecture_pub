import re
from dataclasses import dataclass, field
from enum import Enum


NUMBERED_ACTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$") # This regex captures lines that start with a number followed by a period and some text, which is the expected format for scenario steps. For example, it would match "1. Open the homepage" and capture "1" as the step number and "Open the homepage" as the step text. The regex allows for optional whitespace at the beginning and end of the line, and it is case-insensitive due to re.IGNORECASE.
EXPECTED_RESULT_RE = re.compile( #captures lines that start with "Expected Result:" or "Expected Result for step N:" followed by the expected result text. It captures the optional step number and the expected result text separately. For example, it would match "Expected Result for step 2: The dashboard should load" and capture "2" as the step number and "The dashboard should load" as the expected result text. It would also match "Expected Result: The login button should be visible" and capture an empty string for the step number and "The login button should be visible" as the expected result text. The regex allows for optional whitespace at the beginning and end of the line, and it is case-insensitive due to re.IGNORECASE.
    r"^\s*Expected Result(?: for step (\d+))?:\s+(.+?)\s*$",
    re.IGNORECASE,
)
EXPECTED_ITEM_PREFIX_RE = re.compile(r"^\s*(?:\d+[\).\-\s]+|[-*]\s+)(.+?)\s*$") # This captures common bullet or numbering prefixes that might be used in Xray cells to format expected results. It looks for lines that start with optional whitespace, followed by either a number with a period, parenthesis, dash, or just a bullet character like "-" or "*", and then captures the actual text of the expected result after that prefix. For example, it would match "1. The dashboard should load" and capture "The dashboard should load", or "- The login button should be visible" and capture "The login button should be visible". This allows the static analysis to cleanly extract the expected result text without any formatting characters that are only meant for presentation in Xray.


class StaticExecutionStatus(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    UNSUPPORTED = "unsupported"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class ScenarioStep:
    number: int
    text: str


@dataclass(frozen=True)
class StaticToolDefinition:
    name: str
    description: str
    patterns: tuple[re.Pattern[str], ...]

    def matches(self, step_text: str) -> bool:
        # A tool matches when any of its natural-language patterns fits the step.
        # So a step like "Click the Login button" would match the click_element tool because it contains the word "click" and "button", which are part of the tool's patterns. The patterns are designed to be broad enough to capture common ways of expressing the intent for that tool, while still being specific enough to avoid false positives on unrelated steps. The matching is case-insensitive due to the use of re.IGNORECASE when compiling the patterns.
        return any(pattern.search(step_text) for pattern in self.patterns)


@dataclass
class StaticExecutionResult:
    status: StaticExecutionStatus
    summary: str
    app_name: str = ""
    matched_tools: list[str] = field(default_factory=list) # default_factory is used to ensure that each instance of StaticExecutionResult gets its own separate list, preventing unintended shared state between instances.
    unsupported_steps: list[ScenarioStep] = field(default_factory=list)
    error_message: str = ""
    report_text: str = ""
    failed_tool: str = ""
    failed_locator_key: str = ""
    tried_selectors: list[str] = field(default_factory=list)
    page_url: str = ""
    html_excerpt: str = ""
    healing_attempted: bool = False
    healing_patch: dict = field(default_factory=dict)
    tool_overrides: dict[int, str] = field(default_factory=dict)
    # True when the failure happened while running an Expected Result assertion (not an action step).
    failure_from_expected_validation: bool = False


@dataclass
class StaticLocatorError(RuntimeError):
    # Structured execution failure used by static web tools when an element,
    # locator, or assertion cannot be resolved successfully.
    message: str
    # Static tool that failed, such as "click_element" or "assert_input_value".
    tool_name: str
    # Stable key used to look up or heal locators for this failed target.
    locator_key: str
    # Human-readable selector attempts tried before failing.
    tried_selectors: list[str] = field(default_factory=list)
    # Low-level selector errors collected while trying fallback locators.
    selector_errors: list[str] = field(default_factory=list)
    # True when the failure is an assertion mismatch rather than only a missing locator.
    assertion_failed: bool = False

    def __str__(self) -> str:
        # Include structured fields in the exception text so logs and reports keep
        # enough context for debugging and MCP locator healing.
        details = (
            f"{self.message} | failed_tool={self.tool_name} | "
            f"locator_key={self.locator_key} | tried_selectors={self.tried_selectors}"
        )
        if self.selector_errors:
            details += f" | selector_errors={self.selector_errors}"
        return details


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


WEB_TOOLBOX: tuple[StaticToolDefinition, ...] = (
    StaticToolDefinition(
        name="navigate_to_url",
        description="Open a URL in the browser.",
        patterns=(
            _compile(r"\b(open|go to|navigate to|visit|load)\b.*https?://"),
            _compile(r"\b(aç|git|ziyaret et)\b.*https?://"),
            _compile(r"https?://\S+"),
        ),
    ),
    StaticToolDefinition(
        name="reload_page",
        description="Reload the current page.",
        patterns=(
            _compile(r"\b(reload|refresh)\b.*\b(page|site)?\b"),
            _compile(r"\b(yenile|sayfayi yenile|sayfayı yenile)\b"),
        ),
    ),
    StaticToolDefinition(
        name="go_back",
        description="Navigate one page back in browser history.",
        patterns=(
            _compile(r"\b(go back|back to previous|previous page)\b"),
            _compile(r"\b(geri git|onceki sayfa|önceki sayfa)\b"),
        ),
    ),
    StaticToolDefinition(
        name="go_forward",
        description="Navigate one page forward in browser history.",
        patterns=(
            _compile(r"\b(go forward|forward page|next browser page)\b"),
            _compile(r"\b(ileri git|sonraki tarayici sayfasi|sonraki tarayıcı sayfası)\b"),
        ),
    ),
    StaticToolDefinition(
        name="wait_for_page_load",
        description="Wait until the page reaches a loaded or network-idle state.",
        patterns=(
            _compile(r"\b(wait for page|page loads|page load|loaded)\b"),
            _compile(r"\b(sayfa yuklen|sayfa yüklen|yuklenmesini bekle|yüklenmesini bekle)\b"),
        ),
    ),
    StaticToolDefinition(
        name="wait_for_element",
        description="Wait for a generic element, selector, label, role, or text to appear.",
        patterns=(
            _compile(r"\b(wait for|until)\b.*\b(element|button|link|field|input|selector)\b"),
            _compile(r"\b(bekle)\b.*\b(element|buton|link|alan|input|selector)\b"),
        ),
    ),
    StaticToolDefinition(
        name="wait_for_text",
        description="Wait for specific text to become visible on the page.",
        patterns=(
            _compile(r"\b(wait for|until)\b.*\b(text|message|label|word)\b"),
            _compile(r"\b(bekle)\b.*\b(metin|yazi|yazı|mesaj)\b"),
        ),
    ),
    StaticToolDefinition(
        name="wait",
        description="Wait for a short fixed duration.",
        patterns=(
            _compile(r"\b(wait|pause|sleep)\b.*\b(\d+)\b"),
            _compile(r"\b(\d+)\s*(second|seconds|sec|saniye)\b.*\b(wait|bekle)\b"),
        ),
    ),
    StaticToolDefinition(
        name="click_element",
        description="Click an element by selector, visible text, role name, label, or placeholder.",
        patterns=(
            _compile(r"\b(click|tap|press|select)\b.*"),
            _compile(r".*\b(tikla|tıkla|bas|sec|seç)\b"),
        ),
    ),
    StaticToolDefinition(
        name="double_click_element",
        description="Double-click an element.",
        patterns=(
            _compile(r"\b(double click|dblclick)\b"),
            _compile(r"\b(cift tikla|çift tıkla)\b"),
        ),
    ),
    StaticToolDefinition(
        name="right_click_element",
        description="Right-click an element.",
        patterns=(
            _compile(r"\b(right click|context click)\b"),
            _compile(r"\b(sag tikla|sağ tıkla)\b"),
        ),
    ),
    StaticToolDefinition(
        name="hover_element",
        description="Hover over an element.",
        patterns=(
            _compile(r"\b(hover|mouse over|move over)\b"),
            _compile(r"\b(ustune gel|üstüne gel|hover yap)\b"),
        ),
    ),
    StaticToolDefinition(
        name="focus_element",
        description="Focus an element.",
        patterns=(
            _compile(r"\b(focus)\b.*\b(element|field|input)?\b"),
            _compile(r"\b(odaklan|focusla)\b"),
        ),
    ),
    StaticToolDefinition(
        name="fill_input",
        description="Fill an input, textarea, or editable field.",
        patterns=(
            _compile(r"\b(fill|enter|input|set|write)\b.*\b(field|input|text\s*box|textbox|textarea|value|with)\b"),
            _compile(r"\b(send\s*keys?|sendkeys)\b.*\b(field|input|text\s*box|textbox|textarea|into|to)\b"),
            _compile(r"\b(yaz|doldur|gir)\b.*\b(alan|input|deger|değer)\b"),
        ),
    ),
    StaticToolDefinition(
        name="clear_input",
        description="Clear an input, textarea, or editable field.",
        patterns=(
            _compile(r"\b(clear|empty|delete)\b.*\b(field|input|text\s*box|textbox|textarea|value)\b"),
            _compile(r"\b(temizle|bosalt|boşalt)\b.*\b(alan|input|deger|değer)\b"),
        ),
    ),
    StaticToolDefinition(
        name="type_text",
        description="Type text into the active field or a target field.",
        patterns=(
            _compile(r"\b(type)\b.*\b(text|into)?\b"),
            _compile(r"\b(send\s*keys?|sendkeys)\b.*\b(text|characters|chars|string|into|to)\b"),
            _compile(r"\b(metin yaz|yazi yaz|yazı yaz)\b"),
        ),
    ),
    StaticToolDefinition(
        name="press_key",
        description="Press a keyboard key such as Enter, Tab, or Escape.",
        patterns=(
            _compile(r"\b(press|hit)\b.*\b(enter|tab|escape|esc|space|backspace|delete)\b"),
            _compile(r"\b(enter|tab|escape|esc|space|backspace|delete)\b.*\b(press|key)\b"),
            _compile(r"\b(send\s*keys?|sendkeys)\b.*\b(enter|tab|escape|esc|space|backspace|delete)\b"),
            _compile(r"\b(tusa bas|tuşa bas|enter'a bas|tab'a bas)\b"),
        ),
    ),
    StaticToolDefinition(
        name="check_checkbox",
        description="Check a checkbox or radio option.",
        patterns=(
            _compile(r"\b(check|tick|enable)\b.*\b(checkbox|radio|option)\b"),
            _compile(r"\b(isaretle|işaretle|sec|seç)\b.*\b(checkbox|radio|secenek|seçenek)\b"),
        ),
    ),
    StaticToolDefinition(
        name="uncheck_checkbox",
        description="Uncheck a checkbox.",
        patterns=(
            _compile(r"\b(uncheck|untick|disable)\b.*\b(checkbox|option)\b"),
            _compile(r"\b(isareti kaldir|işareti kaldır|secimi kaldir|seçimi kaldır)\b"),
        ),
    ),
    StaticToolDefinition(
        name="select_option",
        description="Select an option from a select/dropdown control.",
        patterns=(
            _compile(r"\b(select|choose)\b.*\b(option|dropdown|combo|list)\b"),
            _compile(r"\b(sec|seç)\b.*\b(option|dropdown|liste|secenek|seçenek)\b"),
        ),
    ),
    StaticToolDefinition(
        name="upload_file",
        description="Upload a file through a file input.",
        patterns=(
            _compile(r"\b(upload|attach)\b.*\b(file|document|image)\b"),
            _compile(r"\b(dosya yukle|dosya yükle|ekle)\b"),
        ),
    ),
    StaticToolDefinition(
        name="scroll_page",
        description="Scroll the page up, down, to top, or to bottom.",
        patterns=(
            _compile(r"\b(scroll)\b.*\b(page|up|down|top|bottom)?\b"),
            _compile(r"\b(asagi kaydir|aşağı kaydır|yukari kaydir|yukarı kaydır|scroll)\b"),
        ),
    ),
    StaticToolDefinition(
        name="scroll_until_end",
        description="Scroll down until the page reaches the end.",
        patterns=(
            _compile(r"\b(scroll)\b.*\b(until|to|the)?\s*(end|bottom)\b"),
            _compile(r"\b(scroll until the end|scroll to end|scroll to the end)\b"),
            _compile(r"\b(sayfanin sonuna kadar kaydir|sayfa sonuna kadar kaydir)\b"),
        ),
    ),
    StaticToolDefinition(
        name="scroll_to_element",
        description="Scroll until a target element or text is in view.",
        patterns=(
            _compile(r"\b(scroll to|scroll until)\b"),
            _compile(r"\b(olana kadar kaydir|olana kadar kaydır|elemente kaydir|elemente kaydır)\b"),
        ),
    ),
    StaticToolDefinition(
        name="drag_and_drop",
        description="Drag one element and drop it onto another target.",
        patterns=(
            _compile(r"\b(drag|drop|drag and drop)\b"),
            _compile(r"\b(surukle|sürükle|birak|bırak)\b"),
        ),
    ),
    StaticToolDefinition(
        name="find_text",
        description="Find whether text exists on the page.",
        patterns=(
            _compile(r"\b(find|search|look for)\b.*\b(text|message|word|label)\b"),
            _compile(r"\b(ara|bul)\b.*\b(metin|yazi|yazı|mesaj)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_image_visible",
        description="Assert that an image, logo, or icon is visible (img/alt/role).",
        patterns=(
            _compile(
                r"\b(verify|assert|check|ensure|see)\b.*\b(logo|image|icon|picture|grafik|"
                r"görsel|resim|gorsel)\b.*\b(visible|displayed|shown|appear|görünür|gosterilir|gösterilir|"
                r"görüntülen|görüntülendi|görüntülenir|görünüyor|bulunmalı|görülmeli)\b"
            ),
            _compile(
                r"\b(visible|displayed|shown|görünür|görüntülendi|görüntülenir)\b.*\b(logo|image|icon|"
                r"picture|grafik|görsel|resim)\b"
            ),
            _compile(
                r"\b(logo|image|icon|picture)\b.*\b(visible|displayed|shown|appear|görünür|gosterilir|"
                r"gösterilir|görüntülen|görüntülendi|görüntülenir|görünüyor)\b"
            ),
        ),
    ),
    StaticToolDefinition(
        name="assert_image_not_visible",
        description="Assert that an image, logo, or icon is hidden or not shown.",
        patterns=(
            _compile(
                r"\b(verify|assert|check|ensure)?\b.*\b(logo|image|icon|picture|görsel|resim)\b.*"
                r"\b(not visible|not displayed|hidden|absent|görünmez|yok)\b"
            ),
        ),
    ),
    StaticToolDefinition(
        name="assert_text_visible",
        description="Assert that text is visible on the page.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure|should see|see)\b.*\b(text|message|label)\b"),
            _compile(r"\b(gorunur|görünür|gosteriliyor|gösteriliyor)\b.*\b(metin|yazi|yazı|mesaj)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_text_not_visible",
        description="Assert that text is not visible on the page.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(text|message|label)\b.*\b(not visible|not displayed|hidden|absent)\b"),
            _compile(r"\b(metin|yazi|yazı|mesaj)\b.*\b(gorunmuyor|görünmüyor|yok)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_element_visible",
        description="Assert that an element is visible.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(element|button|link|field|input)\b.*\b(visible|displayed|shown)\b"),
            _compile(r"\b(element|buton|link|alan|input)\b.*\b(gorunur|görünür|gosteriliyor|gösteriliyor)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_element_hidden",
        description="Assert that an element is hidden or not visible.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(element|button|link|field|input)\b.*\b(hidden|not visible|not displayed)\b"),
            _compile(r"\b(element|buton|link|alan|input)\b.*\b(gizli|gorunmuyor|görünmüyor)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_checkbox_checked",
        description="Assert that a checkbox or radio option is checked.",
        patterns=(
            _compile(r"\b(verify|assert|ensure)\b.*\b(checkbox|radio|option)\b.*\b(checked|selected|ticked|enabled)\b"),
            _compile(r"\b(checkbox|radio|option)\b.*\b(checked|selected|ticked|enabled)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_checkbox_unchecked",
        description="Assert that a checkbox or radio option is unchecked.",
        patterns=(
            _compile(r"\b(verify|assert|ensure)\b.*\b(checkbox|radio|option)\b.*\b(unchecked|not checked|not be checked|unselected|not selected|not be selected|unticked|not ticked|disabled)\b"),
            _compile(r"\b(checkbox|radio|option)\b.*\b(unchecked|not checked|not be checked|unselected|not selected|not be selected|unticked|not ticked|disabled)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_dropdown_selected",
        description="Assert that a select/dropdown control is selected or focused.",
        patterns=(
            _compile(r"\b(verify|assert|ensure|check)\b.*\b(dropdown|combo|select|list)\b.*\b(selected|focused|active)\b"),
            _compile(r"\b(dropdown|combo|select|list)\b.*\b(selected|focused|active)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_url_contains",
        description="Assert that the current URL contains expected text.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(url|address)\b.*\b(contains|includes)\b"),
            _compile(r"\b(url|adres)\b.*\b(icerir|içerir)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_title_contains",
        description="Assert that the page title contains expected text.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(title)\b.*\b(contains|includes)\b"),
            _compile(r"\b(baslik|başlık|title)\b.*\b(icerir|içerir)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_input_value",
        description="Assert that an input has the expected value.",
        patterns=(
            _compile(r"\b(verify|assert|check|ensure)\b.*\b(input|field|textbox)\b.*\b(value)\b"),
            _compile(r"\b(input|alan)\b.*\b(deger|değer)\b.*\b(kontrol|dogrula|doğrula)\b"),
        ),
    ),
    StaticToolDefinition(
        name="assert_table_columns_populated",
        description="Assert that selected columns are populated in every row of the first visible table.",
        patterns=(
            _compile(
                r"\b(table|grid)\b.*\b(every|each|all)\b.*\b(row|rows)\b.*"
                r"\b(column|columns)\b.*\b(populated|filled|non[- ]?empty|not empty)\b"
            ),
            _compile(
                r"\b(every|each|all)\b.*\b(row|rows)\b.*\b(column|columns)\b.*"
                r"\b(populated|filled|non[- ]?empty|not empty)\b"
            ),
            _compile(
                r"\b(tablo|tablonun|tablodaki)\b.*\b(her|tüm|tum)\b.*"
                r"\b(satır|satir)(da|daki|larda|larda)?\b.*"
                r"\b(kolon|sütun|sutun)(lar|ları|lari)?\b.*"
                r"\b(dolu|boş olmamalı|bos olmamali)\b"
            ),
        ),
    ),
    StaticToolDefinition(
        name="assert_table_column_contains_value",
        description="Assert that every cell in a selected table column contains an expected value.",
        patterns=(
            _compile(
                r"\b(all|every|each)\b.*\b(cells?)\b.*\b(column)\b.*"
                r"\b(contain|contains|include|includes)\b"
            ),
            _compile(
                r"\b(column)\b.*\b(all|every|each)\b.*\b(cells?)\b.*"
                r"\b(contain|contains|include|includes)\b"
            ),
            _compile(
                r"\b(tablo|tablodaki|tablonun)\b.*\b(kolon\w*|sütun\w*|sutun\w*)\b.*"
                r"\b(tüm|tum|her)\b.*\b(hücre|hucre)(ler|leri)?\b.*"
                r"\b(içer|icer|içermeli|icermeli|içermelidir|icermelidir)\b"
            ),
        ),
    ),
    StaticToolDefinition(
        name="get_text",
        description="Read text from an element.",
        patterns=(
            _compile(r"\b(get|read|capture)\b.*\b(text|message|label)\b"),
            _compile(r"\b(metin|yazi|yazı|mesaj)\b.*\b(oku|al)\b"),
        ),
    ),
    StaticToolDefinition(
        name="get_attribute",
        description="Read an attribute from an element.",
        patterns=(
            _compile(r"\b(get|read|capture)\b.*\b(attribute|attr)\b"),
            _compile(r"\b(attribute|ozellik|özellik)\b.*\b(oku|al)\b"),
        ),
    ),
    StaticToolDefinition(
        name="get_input_value",
        description="Read the current value of an input.",
        patterns=(
            _compile(r"\b(get|read|capture)\b.*\b(input|field|textbox)\b.*\b(value)\b"),
            _compile(r"\b(input|alan)\b.*\b(degerini|değerini)\b.*\b(oku|al)\b"),
        ),
    ),
    StaticToolDefinition(
        name="count_elements",
        description="Count elements matching a selector or visible target.",
        patterns=(
            _compile(r"\b(count|number of)\b.*\b(elements|items|rows|buttons|links)\b"),
            _compile(r"\b(kac tane|kaç tane|say)\b.*\b(element|item|satir|satır|buton|link)\b"),
        ),
    ),
    StaticToolDefinition(
        name="accept_dialog",
        description="Accept the next browser dialog.",
        patterns=(
            _compile(r"\b(accept|confirm|ok)\b.*\b(dialog|alert|popup)\b"),
            _compile(r"\b(dialog|alert|popup)\b.*\b(kabul|onayla|tamam)\b"),
        ),
    ),
    StaticToolDefinition(
        name="dismiss_dialog",
        description="Dismiss the next browser dialog.",
        patterns=(
            _compile(r"\b(dismiss|cancel|close)\b.*\b(dialog|alert|popup)\b"),
            _compile(r"\b(dialog|alert|popup)\b.*\b(kapat|iptal|reddet)\b"),
        ),
    ),
    StaticToolDefinition(
        name="take_screenshot",
        description="Take a screenshot of the current page.",
        patterns=(
            _compile(r"\b(screenshot|screen shot|capture screen)\b"),
            _compile(r"\b(ekran goruntusu|ekran görüntüsü|screenshot)\b"),
        ),
    ),
)

TOOL_PRIORITY: tuple[str, ...] = (
    "navigate_to_url",
    "double_click_element",
    "right_click_element",
    "scroll_until_end",
    "scroll_to_element",
    "assert_text_not_visible",
    "assert_image_not_visible",
    "assert_element_hidden",
    "assert_image_visible",
    "assert_text_visible",
    "assert_element_visible",
    "assert_checkbox_unchecked",
    "assert_checkbox_checked",
    "assert_dropdown_selected",
    "assert_input_value",
    "assert_table_column_contains_value",
    "assert_table_columns_populated",
    "assert_url_contains",
    "assert_title_contains",
    "wait_for_text",
    "wait_for_element",
    "select_option",
    "click_element",
    "fill_input",
    "clear_input",
    "type_text",
    "press_key",
    "check_checkbox",
    "uncheck_checkbox",
    "upload_file",
    "hover_element",
    "focus_element",
    "scroll_page",
    "drag_and_drop",
    "find_text",
    "get_attribute",
    "get_input_value",
    "get_text",
    "count_elements",
    "reload_page",
    "go_back",
    "go_forward",
    "wait_for_page_load",
    "wait",
    "accept_dialog",
    "dismiss_dialog",
    "take_screenshot",
)


def get_web_tool_names() -> set[str]:
    return {tool.name for tool in WEB_TOOLBOX}


def resolve_web_tool_names(step_text: str, forced_tool_name: str = "") -> list[str]:
    # Forced routes come from the LLM router and must still reference known tools.
    if forced_tool_name:
        return [forced_tool_name] if forced_tool_name in get_web_tool_names() else []
    matched = {tool.name for tool in WEB_TOOLBOX if tool.matches(step_text)} # returns a set of tool names if any of the tool's patterns match the step text
                                                                             # such as matched= {"click_element", "wait_for_element"}
    return [tool_name for tool_name in TOOL_PRIORITY if tool_name in matched]

def parse_scenario_steps(scenario_text: str) -> list[ScenarioStep]:
    # this function looks for lines in the scenario text that match the format of a numbered step, such as "1. Open the homepage". It uses the NUMBERED_ACTION_RE regex to identify these lines and extract the step number and text. The extracted steps are returned as a list of ScenarioStep objects, which contain the step number and the corresponding text. Lines that do not match the expected format are ignored, allowing for flexibility in how the scenario is written while still capturing the essential steps for static analysis.
    steps: list[ScenarioStep] = []
    for line in scenario_text.splitlines():
        match = NUMBERED_ACTION_RE.match(line)
        if not match:
            continue
        steps.append(ScenarioStep(number=int(match.group(1)), text=match.group(2).strip()))
    return steps # such as "1. Open the homepage" would be parsed into ScenarioStep(number=1, text="Open the homepage").


def parse_expected_results(scenario_text: str) -> dict[int, list[str]]:
    # This function extracts the expected results from the scenario text and organizes them by the corresponding step numbers. 
    # 1. Open login page
    # Expected Result for step 1: Login page is displayed
    # Username field is visible
    # Password field is visible

    # 2. Click login button
    # Expected Result for step 2: Dashboard is displayed
    expected_by_step: dict[int, list[str]] = {}
    current_step_no: int | None = None
    for line in scenario_text.splitlines():
        match = EXPECTED_RESULT_RE.match(line) # captures expected results.
        if match:
            step_no = match.group(1) # captures the expected result step number.
            expected_text = match.group(2).strip() # captures the expected result text.
            if not step_no or not expected_text:
                current_step_no = None
                continue
            current_step_no = int(step_no)
            expected_by_step.setdefault(current_step_no, []).extend(
                split_expected_result_items(expected_text)
            )
            continue

        stripped = line.strip()
        if current_step_no is None or not stripped:
            if not stripped:
                current_step_no = None
            continue
        if NUMBERED_ACTION_RE.match(stripped): # if a new step starts, we stop associating expected results with the previous step.
            current_step_no = None
            continue
        expected_by_step.setdefault(current_step_no, []).extend(
            split_expected_result_items(stripped)
        )
    return expected_by_step
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



def split_expected_result_items(expected_text: str) -> list[str]:
    # Example input:
    #   '1. Login page is displayed\n2. Username field is visible'
    # Expected output:
    #   ['Login page is displayed', 'Username field is visible']
    items: list[str] = []
    # Split the Expected Result text into physical lines.
    # Example: 'A\nB' -> ['A', 'B']
    for line in expected_text.splitlines():
        # Remove surrounding spaces from the current line.
        # Example: '  1. Login page is displayed  ' -> '1. Login page is displayed'
        stripped = line.strip()
        # Ignore empty lines so they do not become empty assertion items.
        # Example: '' or '   ' -> skipped
        if not stripped:
            continue
        # Expected Result lines may come from bulleted/numbered Xray cells.
        # Remove that presentation prefix so assertions keep only their target text.
        # Example: '2. Username field is visible' -> match group 1 is 'Username field is visible'
        # Example: '- Password field is visible' -> match group 1 is 'Password field is visible'
        match = EXPECTED_ITEM_PREFIX_RE.match(stripped)
        # If a bullet/number prefix exists, append only the cleaned assertion text.
        # Otherwise, append the stripped line as-is.
        # Example with match: '1. Login page is displayed' -> 'Login page is displayed'
        # Example without match: 'Dashboard is displayed' -> 'Dashboard is displayed'
        items.append(match.group(1).strip() if match else stripped)
    # Return all assertion items that belong to the Expected Result block.
    # Example: ['Login page is displayed', 'Username field is visible']
    return items


def analyze_web_static_plan(
    scenario_text: str,
    tool_overrides: dict[int, str] | None = None,
) -> StaticExecutionResult:
    steps = parse_scenario_steps(scenario_text) # such as "1. Open the homepage" would be parsed into ScenarioStep(number=1, text="Open the homepage").
    tool_overrides = tool_overrides or {}
    if not steps:
        return StaticExecutionResult(
            status=StaticExecutionStatus.UNSUPPORTED,
            summary="No numbered scenario steps were found for static routing.",
            tool_overrides=tool_overrides,
        )

    matched_tools: list[str] = []
    unsupported_steps: list[ScenarioStep] = []

    for step in steps:
        # step_matches such as {"click_element", "wait_for_element"} for a step like "Click the Login button and wait for the dashboard to load"
        step_matches = resolve_web_tool_names(
            step.text,
            forced_tool_name=tool_overrides.get(step.number, ""),
        )
        if not step_matches:
            unsupported_steps.append(step)
            continue
        for tool_name in step_matches:
            if tool_name not in matched_tools:
                matched_tools.append(tool_name) # we want to keep the order of first matches according to the step sequence, but without duplicates. 

    if unsupported_steps:
        return StaticExecutionResult(
            status=StaticExecutionStatus.UNSUPPORTED,
            summary="At least one scenario step is not covered by the current web toolbox.",
            matched_tools=matched_tools,
            unsupported_steps=unsupported_steps,
            tool_overrides=tool_overrides,
        )

    return StaticExecutionResult(
        status=StaticExecutionStatus.SHADOW,
        summary="All scenario steps match the current generic web toolbox.",
        matched_tools=matched_tools,
        tool_overrides=tool_overrides,
    )


def analyze_static_web_tool_coverage(scenario_text: str, mode: str = "off") -> StaticExecutionResult:
    # Mode-aware static toolbox coverage check.
    # This does not launch a browser; it only analyzes whether the current generic
    # static tools can understand the scenario steps.
    normalized_mode = (mode or "off").strip().lower() 
    if normalized_mode == "off":
        # Static toolbox is intentionally disabled.
        return StaticExecutionResult(
            status=StaticExecutionStatus.DISABLED,
            summary="Static toolbox is disabled.",
        )

    # Analyze which scenario steps are covered or unsupported by the static toolbox.
    analysis = analyze_web_static_plan(scenario_text)
    if normalized_mode == "shadow":
        # Shadow mode reports coverage only; no real browser execution is attempted.
        analysis.status = StaticExecutionStatus.SHADOW
        analysis.summary = f"Static toolbox shadow analysis: {analysis.summary}"
        return analysis

    if normalized_mode == "on":
        # In "on" mode this function is only a dry coverage check. Real execution
        # happens in execute_static_web_flow.
        if analysis.status == StaticExecutionStatus.UNSUPPORTED:
            return analysis
        analysis.status = StaticExecutionStatus.SHADOW
        analysis.summary = "Static toolbox can execute this scenario with generic web tools."
        return analysis

    # Unknown modes are treated as disabled so callers can report the configuration issue.
    return StaticExecutionResult(
        status=StaticExecutionStatus.DISABLED,
        summary=f"Unknown STATIC_TOOLBOX_MODE value '{mode}'. Static toolbox was skipped.",
    )
