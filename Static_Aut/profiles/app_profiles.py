import json
from dataclasses import dataclass
from pathlib import Path

from Resources.config import Config


PROFILES_DIR = Config.PROJECT_ROOT / "Static_Aut" / "profiles" / "definitions"


@dataclass(frozen=True)
class AppProfile:
    app_name: str
    locator_file: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "AppProfile":
        return cls(
            app_name=str(payload.get("app_name", "")).strip(),
            locator_file=str(payload.get("locator_file", "")).strip(),
        )

    def validate(self) -> None:
        if not self.app_name:
            raise ValueError("App profile app_name is required")
        if not self.locator_file:
            raise ValueError(f"App profile {self.app_name!r} must define locator_file")


class AppProfileRegistry:
    def __init__(self, profiles_dir: Path = PROFILES_DIR) -> None:
        self.profiles_dir = profiles_dir
        self._profiles = self._load_profiles()

    def get(self, app_name: str) -> AppProfile:
        profile = self._profiles.get(app_name)
        if profile:
            return profile
        raise KeyError(f"Unknown app profile: {app_name}")

    def all(self) -> list[AppProfile]:
        return list(self._profiles.values())

    def resolve_for_scenario(self, scenario_text: str) -> AppProfile:
        return self.default()

    def default(self) -> AppProfile:
        configured_app_name = (Config.STATIC_DEFAULT_APP_NAME or "").strip()
        # self._profiles is such as {"herokuapp": AppProfile(app_name="herokuapp", locator_file="herokuapp_locators.json"), ...}
        if configured_app_name and configured_app_name in self._profiles:
            return self._profiles[configured_app_name]
        if configured_app_name: # if there is a configured app name but it is not found in the loaded profiles, we raise an error with the list of available profiles for easier debugging.
            available = ", ".join(sorted(self._profiles)) or "none"
            raise ValueError(
                f"STATIC_DEFAULT_APP_NAME={configured_app_name!r} does not match any app profile. "
                f"Available profiles: {available}"
            )
        raise ValueError("STATIC_DEFAULT_APP_NAME must be set to one of the app profile names")

    def _load_profiles(self) -> dict[str, AppProfile]:
        profiles = {}
        if not self.profiles_dir.exists():
            return profiles

        for path in self.profiles_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            profile = AppProfile.from_dict(payload)
            profile.validate()
            profiles[profile.app_name] = profile
        return profiles

app_profile_registry = AppProfileRegistry()
