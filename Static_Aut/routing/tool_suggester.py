import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from LLMs.modelClient import ModelClient
from Resources.config import Config
from Static_Aut.toolbox.static_toolbox import ScenarioStep, WEB_TOOLBOX


SUGGESTIONS_DIR = Config.PROJECT_ROOT / "Static_Aut" / "routing" / "tool_suggestions"


@dataclass(frozen=True)
class StaticToolSuggestion:
    unsupported_step_no: int
    unsupported_step_text: str
    suggested_tool_name: str
    intent: str
    required_locators: list[str] = field(default_factory=list)
    executor_pseudocode: list[str] = field(default_factory=list)
    validation_strategy: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict, step: ScenarioStep) -> "StaticToolSuggestion":
        return cls(
            unsupported_step_no=step.number,
            unsupported_step_text=step.text,
            suggested_tool_name=_snake_case(str(payload.get("suggested_tool_name", ""))),
            intent=str(payload.get("intent", "")).strip(),
            required_locators=_string_list(payload.get("required_locators", [])),
            executor_pseudocode=_string_list(payload.get("executor_pseudocode", [])),
            validation_strategy=str(payload.get("validation_strategy", "")).strip(),
            notes=str(payload.get("notes", "")).strip(),
        )

    def validate(self) -> None:
        if not self.suggested_tool_name:
            raise ValueError("suggested_tool_name is required")
        if not self.intent:
            raise ValueError("intent is required")

    def to_dict(self) -> dict:
        return {
            "unsupported_step_no": self.unsupported_step_no,
            "unsupported_step_text": self.unsupported_step_text,
            "suggested_tool_name": self.suggested_tool_name,
            "intent": self.intent,
            "required_locators": self.required_locators,
            "executor_pseudocode": self.executor_pseudocode,
            "validation_strategy": self.validation_strategy,
            "notes": self.notes,
        }


async def suggest_static_tools(
    test_key: str,
    scenario_text: str,
    unsupported_steps: list[ScenarioStep],
) -> Path | None:
    if not Config.STATIC_TOOL_SUGGESTIONS_ENABLED or not unsupported_steps:
        return None
    
    suggestions = await asyncio.to_thread(
        _request_tool_suggestions,
        scenario_text,
        unsupported_steps,
    )
    return _write_suggestions(test_key, suggestions)
    
#     suggestions = [
#     StaticToolSuggestion(
#         unsupported_step_no=2,
#         unsupported_step_text='Upload file "resume.pdf"',
#         suggested_tool_name="upload_file",
#         intent="Upload a local file into a file input control.",
#         required_locators=["file_input"],
#         executor_pseudocode=[
#             "Find the file input.",
#             "Set the requested file path on the input."
#         ],
#         validation_strategy="Verify that the uploaded file name is visible.",
#         notes="Use Playwright set_input_files."
#     )
# ]



def _request_tool_suggestions(
    scenario_text: str,
    unsupported_steps: list[ScenarioStep],
) -> list[StaticToolSuggestion]:
    existing_tools = [
        {"name": tool.name, "description": tool.description}
        for tool in WEB_TOOLBOX
    ]
    prompt_payload = {
        "scenario": scenario_text,
        "unsupported_steps": [
            {"number": step.number, "text": step.text}
            for step in unsupported_steps
        ],
        "existing_static_tools": existing_tools,
        "instructions": (
            "Suggest static web automation tools for unsupported action steps or non-executable Expected Result assertions. "
            "For Expected Result gaps, prefer assertion/validation tools that deterministically verify the stated outcome. "
            "Do not generate executable Python code. Return implementation specs only."
        ),
    }

    response = ModelClient.json_chat_completion(
        messages=[
            {
                "role": "system",
                "content": (
                    "You design specs for deterministic static web automation tools. "
                    "Unsupported items may be browser actions or Expected Result assertions. "
                    "Return JSON with key 'suggestions'. "
                    "'suggestions' must be a JSON array. "
                    "If there are no suggestions, return {\"suggestions\": []}. "
                    "Each item in 'suggestions' must include: "
                    "unsupported_step_no, suggested_tool_name, intent, required_locators, "
                    "executor_pseudocode, validation_strategy, notes. "
                    "Do not return runnable code or markdown."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False),
            },
        ],
    )

    raw_content = response.choices[0].message.content or "{}"
    payload = json.loads(raw_content)
    raw_suggestions = payload.get("suggestions", [])
    if not isinstance(raw_suggestions, list):
        raise ValueError("Tool suggestion response must contain a suggestions list")

    steps_by_no = {step.number: step for step in unsupported_steps}
    suggestions = []
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        step_no = int(item.get("unsupported_step_no") or 0)
        step = steps_by_no.get(step_no)
        if step is None:
            continue
        suggestion = StaticToolSuggestion.from_dict(item, step)
        suggestion.validate()
        suggestions.append(suggestion)
    return suggestions
    


def _write_suggestions(test_key: str, suggestions: list[StaticToolSuggestion]) -> Path:
    SUGGESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = SUGGESTIONS_DIR / f"{_snake_case(test_key)}_tool_suggestions.json"
    payload = {
        "test_key": test_key,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "suggested",
        "suggestions": [suggestion.to_dict() for suggestion in suggestions],
    }
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return output_path


def _snake_case(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_").lower()
    return value


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
