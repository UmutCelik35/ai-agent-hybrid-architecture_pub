from dataclasses import dataclass

from Static_Aut.toolbox.static_toolbox import ScenarioStep


@dataclass(frozen=True)
class StepContext:
    step: ScenarioStep
    tool_name: str
    test_key: str
    app_name: str = ""
