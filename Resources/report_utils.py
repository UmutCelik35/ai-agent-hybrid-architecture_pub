import json
import re
from dataclasses import dataclass

from autogen_agentchat.base import TaskResult

from Jira_Aut.jira_bug_client import JiraBugClient


@dataclass
class TokenUsageSummary:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def extract_last_text_message(task_result: TaskResult) -> str: # This function iterates through the messages in the TaskResult in reverse order, looking for the last message that contains a non-empty string content. It returns that content as a string. If no such message is found, it returns an empty string.
    for message in reversed(task_result.messages):  # messages means the list of all messages generated during the execution of a task, including system messages, agent messages, and any other relevant messages. We reverse the list to start checking from the most recent message. 
        content = getattr(message, "content", None) # We use getattr to safely get the content attribute, in case it's missing or not a string. If content is a non-empty string, we return it. Otherwise, we continue searching.
                                                    # content defines the actual text content of the message, which is what we want to extract. We check if it's a string and not just whitespace before returning it. If it's empty or not a string, we ignore it and keep looking through the previous messages.
        if isinstance(content, str) and content.strip(): # We check if content is a string and not just whitespace. If it is valid, we return it.
            return content
    return ""


def calculate_token_usage(task_result: TaskResult) -> TokenUsageSummary:
    summary = TokenUsageSummary()

    for message in task_result.messages:
        usage = getattr(message, "models_usage", None)
        if usage is None:
            continue
        summary.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        summary.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

    return summary


def normalize_automation_report_text(raw_text: str) -> str:
    # Nothing to normalize if the agent did not return any report text.
    if not raw_text:
        return raw_text

    # If the report already has the markers expected by JiraBugClient, keep it unchanged.
    if JiraBugClient.REPORT_START in raw_text and JiraBugClient.REPORT_END in raw_text:
        return raw_text

    # Some agents return only a JSON object, or a JSON object inside prose/fences.
    # Extract that payload so we can wrap it in the standard automation report markers.
    payload = _extract_json_payload(raw_text)
    if payload is None:
        return raw_text

    # Reformat the payload for stable parsing and preserve the completion marker if present.
    report_json = json.dumps(payload, ensure_ascii=False, indent=2)
    suffix = "AUTOMATION COMPLETED" if "AUTOMATION COMPLETED" in raw_text else ""
    return (
        f"{JiraBugClient.REPORT_START}\n"
        f"{report_json}\n"
        f"{JiraBugClient.REPORT_END}\n"
        f"{suffix}"
    )


def _extract_json_payload(raw_text: str) -> dict | None:
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    candidates = []
    if fenced_match:
        candidates.append(fenced_match.group(1))

    first_brace = raw_text.find("{")
    if first_brace >= 0:
        candidates.append(raw_text[first_brace:])

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            payload, _ = decoder.raw_decode(candidate.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    return None


def append_token_usage_to_report(raw_text: str, usage: TokenUsageSummary) -> str:
    raw_text = normalize_automation_report_text(raw_text)
    if JiraBugClient.REPORT_START not in raw_text or JiraBugClient.REPORT_END not in raw_text:
        return raw_text

    start_part, rest = raw_text.split(JiraBugClient.REPORT_START, 1)
    _, end_part = rest.split(JiraBugClient.REPORT_END, 1)
    try:
        payload = JiraBugClient.extract_report_payload(raw_text)
    except Exception:
        return raw_text

    payload["token_usage"] = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }

    rebuilt_json = JiraBugClient._repair_common_json_escapes(json.dumps(payload, ensure_ascii=False, indent=2))
    return f"{start_part}{JiraBugClient.REPORT_START}\n{rebuilt_json}\n{JiraBugClient.REPORT_END}{end_part}"
