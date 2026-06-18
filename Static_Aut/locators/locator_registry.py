import json
from pathlib import Path

from Static_Aut.profiles.app_profiles import app_profile_registry


LOCATOR_DIR = Path(__file__).resolve().parent
LOCATOR_DEFINITIONS_DIR = LOCATOR_DIR / "definitions"
HEALED_LOCATORS_PATH = LOCATOR_DIR / "healed_locators.json"


class LocatorRegistry:
    def __init__(
        self,
        base_path: Path | None = None,
        healed_path: Path = HEALED_LOCATORS_PATH,
    ) -> None:
        self.base_path = base_path
        self.healed_path = healed_path
        self._locators = self._load_locators()

    def get(self, app_name: str, locator_key: str) -> list[str]:
        app_locators = self._locators.get(app_name, {})
        return self._normalize_selectors(app_locators.get(locator_key, []))
        # returns such as ["#login-button", "button[type='submit']"]

    def save_healed_selector(self, app_name: str, locator_key: str, selector: str) -> None:
        selector = selector.strip()
        if not selector:
            raise ValueError("Healed selector cannot be empty")

        # selector = "#new-login"
        # existing_selectors = ["#old-login", "button[type='submit']"]
        # app_locators["button__login"] = ["#new-login", "#old-login", "button[type='submit']"]
        healed_locators = self._read_json(self.healed_path)
        app_locators = dict(healed_locators.get(app_name, {}))
        existing_selectors = self._normalize_selectors(app_locators.get(locator_key, []))
        app_locators[locator_key] = list(dict.fromkeys([selector, *existing_selectors])) # dict.fromkeys cleans duplications
        healed_locators[app_name] = app_locators # updated healed_locators dict with the new healed selector

        self.healed_path.parent.mkdir(parents=True, exist_ok=True) # create the directory if it does not exist
        with self.healed_path.open("w", encoding="utf-8") as handle:
            json.dump(healed_locators, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        base_locators = self._load_base_locators()
        self._locators = self._merge_locators(base_locators, healed_locators) # merge the base locators with the updated healed locators and update the in-memory registry

    def apply_runtime_selector(self, app_name: str, locator_key: str, selector: str) -> None:
        selector = selector.strip()
        if not selector:
            raise ValueError("Runtime selector cannot be empty")

        app_locators = dict(self._locators.get(app_name, {})) # get the current locators for the app from the in-memory registry
        #         app_locators = {
        #     "button__login": ["#old-login"]
        # } 
        existing_selectors = self._normalize_selectors(app_locators.get(locator_key, [])) # existing_selectors = ["#old-login"]
        # selector = "#new-login"
        # existing_selectors = ["#old-login", "button[type='submit']"]
        # app_locators[locator_key] = ["#new-login", "#old-login", "button[type='submit']"]
        app_locators[locator_key] = list(dict.fromkeys([selector, *existing_selectors])) 
        self._locators[app_name] = app_locators

    def _load_locators(self) -> dict:
        base_locators = self._load_base_locators()
        healed_locators = self._read_json(self.healed_path)
        return self._merge_locators(base_locators, healed_locators)

    def _load_base_locators(self) -> dict:
        # Start with an optional explicit locator file, mostly useful for tests
        # or custom registry instances. Normal runtime usually leaves base_path empty.
        locators = self._read_json(self.base_path) if self.base_path else {}
        # Load every locator definition file declared by the app profiles.
        for profile in app_profile_registry.all():
            profile_path = LOCATOR_DEFINITIONS_DIR / profile.locator_file
            # If the explicit base_path is also one of the profile files, avoid reading it twice.
            if profile_path == self.base_path:
                continue
            # profile_locators is the JSON payload for one app/profile file.
            # locators is the accumulated registry across all profiles.
            profile_locators = self._read_json(profile_path)
            locators = self._merge_locators(locators, profile_locators)
        return locators

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            return {}
        raw_text = path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return {}
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Locator JSON file is invalid: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Locator JSON file must contain an object: {path}")
        return payload

    @classmethod
    def _merge_locators(cls, base_locators: dict, healed_locators: dict) -> dict:
        merged = dict(base_locators)
        for app_name, healed_app_locators in healed_locators.items():
            app_locators = dict(merged.get(app_name, {}))
            for locator_key, healed_selectors in healed_app_locators.items():
                base_selectors = cls._normalize_selectors(app_locators.get(locator_key, []))
                healed_selectors = cls._normalize_selectors(healed_selectors)
                app_locators[locator_key] = list(dict.fromkeys(
                    [*healed_selectors, *base_selectors]
                ))
            merged[app_name] = app_locators
        return merged

    @staticmethod
    def _normalize_selectors(selectors) -> list[str]:
        if isinstance(selectors, str):
            selectors = [selectors]
        if not isinstance(selectors, list):
            return []
        return [str(selector).strip() for selector in selectors if str(selector).strip()]


locator_registry = LocatorRegistry()
