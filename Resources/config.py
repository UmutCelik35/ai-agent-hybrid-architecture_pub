# config.py
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class Config:
    PROJECT_ROOT = PROJECT_ROOT
    SCENARIOS_DIR = PROJECT_ROOT / "Scenarious"
    RESOURCES_DIR = PROJECT_ROOT / "Resources"

    # Google
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    # OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    # LLM Model
    LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "")

    # Xray deployment
    XRAY_DEPLOYMENT = os.getenv("XRAY_DEPLOYMENT", "datacenter").strip().lower()

    # Jira Data Center
    JIRA_DATACENTER_URL = os.getenv("JIRA_DATACENTER_URL", "")
    JIRA_DATACENTER_API_TOKEN = os.getenv("JIRA_DATACENTER_API_TOKEN", "")
    JIRA_DATACENTER_VERIFY_SSL = _env_bool("JIRA_DATACENTER_VERIFY_SSL", default=False)

    # Jira Cloud
    JIRA_CLOUD_URL = os.getenv("JIRA_CLOUD_URL", "")
    JIRA_CLOUD_EMAIL = os.getenv("JIRA_CLOUD_EMAIL", "")
    JIRA_CLOUD_API_TOKEN = os.getenv("JIRA_CLOUD_API_TOKEN", "")

    # Xray Cloud
    XRAY_CLOUD_API_URL = os.getenv("XRAY_CLOUD_API_URL", "https://xray.cloud.getxray.app")
    XRAY_CLOUD_CLIENT_ID = os.getenv("XRAY_CLOUD_CLIENT_ID", "")
    XRAY_CLOUD_CLIENT_SECRET = os.getenv("XRAY_CLOUD_CLIENT_SECRET", "")

    # Bug creation
    BUG_PROJECT_KEY = os.getenv("BUG_PROJECT_KEY", "")
    BUG_ISSUE_TYPE_ID = os.getenv("BUG_ISSUE_TYPE_ID", "1")
    BUG_TYPE_FIELD_ID = os.getenv("BUG_TYPE_FIELD_ID", "")
    BUG_TYPE_OPTION_ID = os.getenv("BUG_TYPE_OPTION_ID", "")
    BUG_CREATION_MODE = os.getenv("BUG_CREATION_MODE", "review")
    BUG_REVIEW_UI_AUTO_OPEN = _env_bool("BUG_REVIEW_UI_AUTO_OPEN", default=True)

    # Static toolbox execution mode
    # off: skip static execution and run the full scenario with MCP
    # shadow: analyze static tool coverage only; no browser execution is started
    # on: run static tools first; locator failures start MCP locator repair and retry static once
    STATIC_TOOLBOX_MODE = os.getenv("STATIC_TOOLBOX_MODE", "on")
    STATIC_PLAYWRIGHT_HEADLESS = _env_bool("STATIC_PLAYWRIGHT_HEADLESS", default=False)
    STATIC_SELF_HEALING_ENABLED = _env_bool("STATIC_SELF_HEALING_ENABLED", default=True)
    STATIC_LLM_ROUTER_ENABLED = _env_bool("STATIC_LLM_ROUTER_ENABLED", default=True)
    STATIC_TOOL_SUGGESTIONS_ENABLED = _env_bool("STATIC_TOOL_SUGGESTIONS_ENABLED", default=True)
    STATIC_DEFAULT_APP_NAME = os.getenv("STATIC_DEFAULT_APP_NAME", "")
    # Retry count for temporary MCP/LLM healing call failures such as rate limits.
    STATIC_HEALING_MAX_RETRIES = int(os.getenv("STATIC_HEALING_MAX_RETRIES", "1"))
    STATIC_HEALING_HTML_LIMIT = int(os.getenv("STATIC_HEALING_HTML_LIMIT", "4000"))

    @staticmethod
    def _validate_static_config():
        if not (Config.STATIC_DEFAULT_APP_NAME or "").strip():
            raise ValueError("STATIC_DEFAULT_APP_NAME must be set in .env")

    @staticmethod
    def _validate_xray_config():
        if Config.XRAY_DEPLOYMENT not in {"datacenter", "cloud"}:
            raise ValueError("XRAY_DEPLOYMENT must be either 'datacenter' or 'cloud'")

        if Config.XRAY_DEPLOYMENT == "datacenter":
            if not Config.JIRA_DATACENTER_URL:
                raise ValueError("JIRA_DATACENTER_URL must be set in .env for Data Center")
            if not Config.JIRA_DATACENTER_API_TOKEN:
                raise ValueError("JIRA_DATACENTER_API_TOKEN must be set in .env for Data Center")
            return

        if not Config.JIRA_CLOUD_URL:
            raise ValueError("JIRA_CLOUD_URL must be set in .env for Cloud")
        if not Config.JIRA_CLOUD_EMAIL:
            raise ValueError("JIRA_CLOUD_EMAIL must be set in .env for Cloud")
        if not Config.JIRA_CLOUD_API_TOKEN:
            raise ValueError("JIRA_CLOUD_API_TOKEN must be set in .env for Cloud")
        if not Config.XRAY_CLOUD_CLIENT_ID:
            raise ValueError("XRAY_CLOUD_CLIENT_ID must be set in .env for Cloud")
        if not Config.XRAY_CLOUD_CLIENT_SECRET:
            raise ValueError("XRAY_CLOUD_CLIENT_SECRET must be set in .env for Cloud")


    @staticmethod
    def _validate_llm_config():
        model_name = (Config.LLM_MODEL_NAME or "").strip().lower()
        if not model_name:
            raise ValueError("LLM_MODEL_NAME must be set in .env")

        if model_name.startswith("gemini"):
            if not Config.GOOGLE_API_KEY:
                raise ValueError("GOOGLE_API_KEY must be set in .env for Gemini models")
            return

        if model_name.startswith("gpt"):
            if not Config.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY must be set in .env for OpenAI models")
            return

    @staticmethod
    def validate():
        Config._validate_llm_config()
        Config._validate_static_config()
        Config._validate_xray_config()

        
