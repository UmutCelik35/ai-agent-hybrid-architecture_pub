import asyncio
from asyncio.log import logger
import json
from dataclasses import dataclass

from LLMs.modelClient import ModelClient
from Static_Aut.toolbox.static_toolbox import ScenarioStep, WEB_TOOLBOX, get_web_tool_names


@dataclass(frozen=True)
class StaticToolRoute:
    step_no: int
    step_text: str
    tool_name: str
    confidence: float
    reason: str = ""

    @classmethod
    def from_dict(cls, payload: dict, step: ScenarioStep) -> "StaticToolRoute":
        return cls(
            step_no=step.number,
            step_text=step.text,
            tool_name=str(payload.get("tool_name", "")).strip(),
            confidence=_parse_confidence(payload.get("confidence")),
            reason=str(payload.get("reason", "")).strip(),
        )

    def validate(self) -> None:
        if self.tool_name not in get_web_tool_names():
            raise ValueError(f"Unknown static tool route: {self.tool_name}")
        if self.confidence < 0.70:
            raise ValueError(f"Static tool route confidence is too low: {self.confidence}")


@dataclass(frozen=True)
class StaticToolRoutingResult:
    routes: list[StaticToolRoute]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property # property means that tool_overrides can be accessed as an attribute (result.tool_overrides) but it will execute the code in the method to compute the value on the fly, which in this case converts the list of routes into a dictionary for easy lookup when executing the static web flow.
    def tool_overrides(self) -> dict[int, str]: # This property converts the list of StaticToolRoute into a dictionary where the key is the step number and the value is the tool name, which can be used as overrides when executing the static web flow.
        return {route.step_no: route.tool_name for route in self.routes}


async def route_unsupported_steps_with_llm(
    scenario_text: str,
    unsupported_steps: list[ScenarioStep],
) -> StaticToolRoutingResult:
    if not unsupported_steps:
        return StaticToolRoutingResult(routes=[])
    return await asyncio.to_thread(
        _request_tool_routes,
        scenario_text,
        unsupported_steps,
    )


def _request_tool_routes(
    scenario_text: str,
    unsupported_steps: list[ScenarioStep],
) -> StaticToolRoutingResult:
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
            "Map each unsupported step to exactly one existing static tool only if "
            "the step can be executed safely by that tool. Do not invent new tools. "
            "If no existing tool fits, omit that step from routes."
        ),
    }

    response = ModelClient.json_chat_completion(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a strict static automation router. "
                    "Return JSON with key 'routes'. "
                    "'routes' must be a JSON array. "
                    "If no existing tool fits, return {\"routes\": []}. "
                    "Each item in 'routes' must include: "
                    "unsupported_step_no, tool_name, confidence, reason. "
                    "tool_name must be one of the provided existing_static_tools. "
                    "Do not return markdown."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False),
            },
        ],
    )
    
    raw_content = response.choices[0].message.content or "{}"
    # raw_content = 
    # """
    # {
    # "routes": [
    #     {
    #     "unsupported_step_no": 2,
    #     "tool_name": "click_element",
    #     "confidence": 0.92,
    #     "reason": "The step asks to click a visible button."
    #     }
    # ]
    # }
    # """

    payload = json.loads(raw_content)
    raw_routes = payload.get("routes", [])
    #     raw_routes = [
    #     {
    #         "unsupported_step_no": 2,
    #         "tool_name": "click_element",
    #         "confidence": 0.92,
    #         "reason": "The step asks to click a visible button."
    #     }
    # ]

    logger.info(f"Received static tool routing response: {payload}")
    logger.info(f"Parsed static tool routes: {raw_routes}")
    
    if not isinstance(raw_routes, list): # check if the raw_routes is a list, if not raise an error because we expect a list of routes to process
        raise ValueError("Static tool routing response must contain a routes list")

    steps_by_no = {step.number: step for step in unsupported_steps}
#     steps_by_no = {
#     2: ScenarioStep(number=2, text='Upload file "resume.pdf"'),
#     5: ScenarioStep(number=5, text='Drag "Card A" to "Done" column'),
# }
    routes: list[StaticToolRoute] = []
    rejected_step_nos: set[int] = set()
    for item in raw_routes:
        if not isinstance(item, dict):
            logger.warning("Skipping invalid static route item because it is not a dict: %s", item)
            continue
        try:
            step_no = int(item.get("unsupported_step_no") or 0)
        except (TypeError, ValueError):
            logger.warning("Skipping static route with invalid unsupported_step_no: %s", item)
            continue
        step = steps_by_no.get(step_no) # ScenarioStep(number=2, text="Click Login button")
        if step is None:
            logger.warning("Skipping static route because unsupported step no was not found: %s", item)
            continue
        # 
        route = StaticToolRoute.from_dict(item, step)
        # route = 
        # StaticToolRoute(
        # step_no=2,
        # step_text='Click "Login" button',
        # tool_name="click_element",
        # confidence=0.92,
        # reason="The step asks to click a visible button."
        #)

        try:
            route.validate()
        except ValueError as exc:
            rejected_step_nos.add(route.step_no)
            logger.warning(
                "Skipping invalid static route | step=%s | tool=%s | confidence=%.2f | reason=%s | error=%s",
                route.step_no,
                route.tool_name,
                route.confidence,
                route.reason,
                exc,
            )
            continue
        routes.append(route)

    accepted_step_nos = {route.step_no for route in routes}
    for step in unsupported_steps:
        if step.number not in accepted_step_nos and step.number not in rejected_step_nos:
            logger.warning(
                "LLM did not return a usable static route for unsupported step | step=%s | text=%s",
                step.number,
                step.text,
            )

    usage = getattr(response, "usage", None)
    return StaticToolRoutingResult(
        routes=routes,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
    )


def _parse_confidence(value) -> float:
    # The router asks the LLM for a confidence score, but LLMs may return either
    # a numeric value like 0.92 or a label like "high". Normalize both styles.
    if isinstance(value, (int, float)):
        return float(value)

    # Convert None/empty/non-numeric values to a lowercase string before parsing.
    normalized = str(value or "").strip().lower()
    if not normalized:
        return 0.0

    # Map common textual confidence labels to numeric thresholds used by validate().
    if normalized in {"high", "very high", "strong"}:
        return 0.90
    if normalized in {"medium", "moderate"}:
        return 0.70
    if normalized in {"low", "weak"}:
        return 0.40

    # If the LLM returned a number as text, such as "0.85", accept it.
    try:
        return float(normalized)
    except ValueError:
        # Unknown labels are treated as 0.0 so the route will be rejected later.
        return 0.0
