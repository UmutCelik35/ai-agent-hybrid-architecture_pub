import json
import re
from dataclasses import dataclass
from pathlib import Path

from Resources.config import Config


PROFILES_DIR = Config.PROJECT_ROOT / "Static_Aut" / "profiles" / "definitions"
LOCATOR_DEFINITIONS_DIR = Config.PROJECT_ROOT / "Static_Aut" / "locators" / "definitions"


@dataclass(frozen=True)
class ProfileDefinition:
    app_name: str
    locator_file: str


def list_profiles() -> list[ProfileDefinition]:
    profiles = []
    if not PROFILES_DIR.exists():
        return profiles

    for path in sorted(PROFILES_DIR.glob("*.json")):
        payload = _read_json(path)
        app_name = str(payload.get("app_name", "")).strip()
        locator_file = str(payload.get("locator_file", "")).strip()
        if app_name and locator_file:
            profiles.append(ProfileDefinition(app_name=app_name, locator_file=locator_file))
    return profiles


def list_profile_names() -> list[str]:
    return [profile.app_name for profile in list_profiles()]


def list_locator_files() -> list[str]:
    if not LOCATOR_DEFINITIONS_DIR.exists():
        return []
    return sorted(path.name for path in LOCATOR_DEFINITIONS_DIR.glob("*.json"))


def get_profile(app_name: str) -> ProfileDefinition | None:
    app_name = app_name.strip()
    for profile in list_profiles():
        if profile.app_name == app_name:
            return profile
    return None


def save_profile(app_name: str, locator_file: str, create_locator: bool = True) -> ProfileDefinition:
    app_name = app_name.strip()
    locator_file = locator_file.strip()
    _validate_app_name(app_name)
    _validate_locator_file_name(locator_file)

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    LOCATOR_DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)

    locator_path = LOCATOR_DEFINITIONS_DIR / locator_file
    if locator_path.exists():
        _validate_locator_payload(locator_path, app_name)
    elif create_locator:
        locator_path.write_text(
            json.dumps({app_name: {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        raise ValueError(f"Locator file does not exist: {locator_file}")

    profile_path = _find_profile_path(app_name) or _profile_path(app_name)
    payload = {
        "app_name": app_name,
        "locator_file": locator_file,
    }
    profile_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ProfileDefinition(app_name=app_name, locator_file=locator_file)


def delete_profile(app_name: str) -> None:
    app_name = app_name.strip()
    _validate_app_name(app_name)
    profile_path = _find_profile_path(app_name) or _profile_path(app_name)
    if profile_path.exists():
        profile_path.unlink()


def default_locator_file_name(app_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", app_name.strip()).strip("_")
    if not cleaned:
        return ""
    return f"{cleaned}_locators.json"


def _profile_path(app_name: str) -> Path:
    return PROFILES_DIR / f"{app_name}.json"


def _find_profile_path(app_name: str) -> Path | None:
    if not PROFILES_DIR.exists():
        return None
    for path in PROFILES_DIR.glob("*.json"):
        try:
            payload = _read_json(path)
        except Exception:
            continue
        if str(payload.get("app_name", "")).strip() == app_name:
            return path
    return None


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_app_name(app_name: str) -> None:
    if not app_name:
        raise ValueError("Profile name is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", app_name):
        raise ValueError("Profile name can only contain letters, numbers, underscore, and dash")


def _validate_locator_file_name(locator_file: str) -> None:
    if not locator_file:
        raise ValueError("Locator file is required")
    if Path(locator_file).name != locator_file or not locator_file.endswith(".json"):
        raise ValueError("Locator file must be a JSON filename, for example AirlinePortal_locators.json")


def _validate_locator_payload(locator_path: Path, app_name: str) -> None:
    payload = _read_json(locator_path)
    if not isinstance(payload, dict):
        raise ValueError(f"Locator file must contain a JSON object: {locator_path.name}")
    if app_name not in payload:
        raise ValueError(
            f"Locator file {locator_path.name} must contain a top-level {app_name!r} key"
        )
