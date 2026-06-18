import re

# Regex (Regular Expression) to find and remove existing numbering or bullets from strings.
# For example, it catches "1. ", "2) ", "- ", or "* " at the beginning of a line.
NUMBERED_STEP_RE = re.compile(r"^\s*(?:\d+[\).\-\s]+|[-*]\s+)(.+?)\s*$")

def _field_raw(step, field_name):
    """
    Safely navigates through a deeply nested dictionary to get the raw text of a field.
    Using .get() prevents the code from crashing (KeyError) if a field is missing.
    """
    return (
        step.get("fields", {})
        .get(field_name, {})
        .get("value", {})
        .get("raw", "")
        .strip()
    )

def _split_action_lines(action_text):
    """
    Takes a chunk of text containing multiple actions, splits it line by line,
    and removes any manual bullet points or numbers so we can re-number them cleanly.
    """
    cleaned = (action_text or "").strip()
    if not cleaned:
        return []

    # Split the text by newlines and ignore any completely empty lines.
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()] # etc. "1.    Open the homepage\n\n\n\n2. Click on login\n\n3. Enter credentials" --> ["1. Open the homepage", "2. Click on login", "3. Enter credentials"] (empty line is removed)
    parsed_lines = []

    for line in lines: # For each line, we want to check if it starts with some kind of numbering or bullet (like "1. ", "- ", "* ", etc.) and remove that so we can reformat it consistently later.
        # Check if the line starts with a bullet point or number using our Regex.
        match = NUMBERED_STEP_RE.match(line) # If it matches, it means the line starts with some kind of numbering or bullet. etc. "1. Open the homepage" or "- Open the homepage" or "* Open the homepage"
        # If it matches, append only the actual text (group 1). If not, append the whole line.
        parsed_lines.append(match.group(1).strip() if match else line) # etc. "1. Open the homepage" --> "Open the homepage"

    return parsed_lines


def _split_expected_lines(expected_text):
    """
    Split a multi-line Expected Result cell into clean assertion lines.
    Numbering/bullets inside the Xray cell are presentation only; keeping them
    would make continuation lines look like new executable scenario steps.
    """
    cleaned = (expected_text or "").strip()
    if not cleaned:
        return []

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    parsed_lines = []

    for line in lines:
        match = NUMBERED_STEP_RE.match(line)
        parsed_lines.append(match.group(1).strip() if match else line)

    return parsed_lines


def format_xray_scenario(test_key, test_summary_payload, steps_payload):
    """
    The main engine. It takes the raw JSON payloads of the test issue and its steps,
    and formats them into a clean, readable step-by-step scenario string.
    """
    # Extract the summary of the issue, provide a fallback if it's missing.
    summary = (
        test_summary_payload.get("fields", {}).get("summary")
        or "No summary provided"
    )
    
    # Extract the list of steps.
    steps = steps_payload.get("steps") or []

    # If there are no steps at all, stop the process and raise an error.
    if not steps:
        raise ValueError(f"No Xray steps found for test issue {test_key}")

    # Prepare the header of our formatted output.
    scenario_lines = [
        f"Test Key: {test_key}",
        f"Test Summary: {summary}",
        "",
        "Execute the following Xray web test scenario exactly and in order:",
        ""
    ]

    # Initialize a master step counter.
    display_step = 1

    # Loop through each step in the payload.
    for xray_step in steps:
        # Extract Action, Data, and Expected Result fields safely.
        action_lines = _split_action_lines(_field_raw(xray_step, "Action")) # This will give us a list of cleaned action lines without numbering or bullets.
        data_text = _field_raw(xray_step, "Data")
        expected_lines = _split_expected_lines(_field_raw(xray_step, "Expected Result"))

        # Skip Xray rows without an Action. Expected Results are validated only
        # when they belong to an executable action step.
        if not action_lines:
            continue

        # Format and append the Action line(s).
        for action_line in action_lines:
            scenario_lines.append(f"{display_step}. {action_line}") # We add our own numbering here based on the order of the steps in the Xray issue, regardless of how the test engineer formatted it. This ensures consistent formatting like "1. Open the homepage", "2. Click on the login button", etc.
            display_step += 1

        # Format and append the Data (e.g., test credentials, inputs).
        if data_text:
            scenario_lines.append(f"Data: {data_text}")

        # Format and append the Expected Result.
        if expected_lines:
            validated_step = display_step - 1
            if validated_step > 0:
                # Keep only the first assertion on the prefixed line. Remaining
                # assertions must be plain continuation lines; otherwise lines
                # like "2. Username field is visible" are parsed as action steps.
                first_expected, *remaining_expected = expected_lines
                scenario_lines.append(f"Expected Result for step {validated_step}: {first_expected}")
                scenario_lines.extend(remaining_expected) # If there are more expected lines, we add them as continuation lines without numbering. This way, if the Expected Result has multiple assertions, only the first one is prefixed with "Expected Result for step X:", and the rest are listed below it without any numbering, ensuring they are not mistaken for new action steps.
            else:
                scenario_lines.append(f"Expected Result: {expected_lines[0]}")
                scenario_lines.extend(expected_lines[1:])

        # Add a blank line for readability between Xray steps.
        scenario_lines.append("")

    # If the counter is still 1 after the loop, it means all steps were completely empty.
    if display_step == 1:
        raise ValueError(f"Xray issue {test_key} did not contain runnable scenario text")

    # Join the list into a single string with newlines and remove trailing whitespace.
    # Example output format:
    # """Test Key: PROJ-123
    # Test Summary: Verify that the user can log in successfully
    #
    # Execute the following Xray web test scenario exactly and in order:
    #
    # 1. Go to the login page
    # Data: username=test_user, password=secret
    # Expected Result for step 1: Login page is displayed
    #
    # 2. Click the login button
    # Expected Result for step 2: Dashboard is displayed"""
    return "\n".join(scenario_lines).strip()



