import os

from autogen_core.models import ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient
from openai import OpenAI

from Resources.config import Config


class ModelClient:
    MODELS = {
        "gpt_4o": {
            "provider": "openai",
            "model": "gpt-4o",
        },
        "gpt_4o_mini": {
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        "gpt_4_1": {
            "provider": "openai",
            "model": "gpt-4.1",
        },
        "gpt_4_1_mini": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
        },
        "gemini_2_5_flash": {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        }
    }
    @staticmethod
    def selected_model_config() -> dict:
        configured_model = (Config.LLM_MODEL_NAME or "").strip()
        if not configured_model:
            raise ValueError("LLM_MODEL_NAME is not defined and cannot be empty.")

        model_config = ModelClient.MODELS.get(configured_model)
        if model_config:
            return dict(model_config)

        raise ValueError(f"Unsupported LLM_MODEL_NAME: {configured_model}")

    @staticmethod
    def selected_provider() -> str:
        return str(ModelClient.selected_model_config()["provider"])

    @staticmethod
    def model_name() -> str:
        return str(ModelClient.selected_model_config()["model"])

    @staticmethod
    def available_models() -> dict[str, dict]:
        return {alias: dict(model_config) for alias, model_config in ModelClient.MODELS.items()}

    @staticmethod
    def openai_api_key() -> str:
        api_key = (Config.OPENAI_API_KEY or "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY .env dosyasinda tanimli olmali ve bos olmamali.")
        return api_key

    @staticmethod
    def google_api_key() -> str:
        api_key = (Config.GOOGLE_API_KEY or "").strip()
        if not api_key:
            raise ValueError("GOOGLE_API_KEY .env dosyasinda tanimli olmali ve bos olmamali.")
        return api_key

    @staticmethod
    def sdk_client() -> OpenAI:
        model_config = ModelClient.selected_model_config()
        provider = model_config["provider"]

        if provider == "openai":
            return OpenAI(api_key=ModelClient.openai_api_key())

        if provider == "gemini":
            return OpenAI(
                api_key=ModelClient.google_api_key(),
                base_url=model_config["base_url"],
            )

        if provider == "ollama":
            return OpenAI(
                api_key="ollama",
                base_url=model_config["base_url"],
            )

        raise ValueError(f"Unsupported LLM provider: {provider}")

    @staticmethod
    def json_chat_completion(messages: list[dict], temperature: float = 0):
        return ModelClient.sdk_client().chat.completions.create(
            model=ModelClient.model_name(),
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=messages,
        )

    @staticmethod
    def openai_json_chat_completion(messages: list[dict], temperature: float = 0):
        return ModelClient.json_chat_completion(messages=messages, temperature=temperature)

    @staticmethod
    def chat_model_client():
        model_config = ModelClient.selected_model_config()
        provider = model_config["provider"]

        if provider == "openai":
            api_key = ModelClient.openai_api_key()
            os.environ["OPENAI_API_KEY"] = api_key
            return ModelClient._openai_compatible_model_client(
                api_key=api_key,
                model=model_config["model"],
                timeout=30.0,
            )

        if provider == "gemini":
            api_key = ModelClient.google_api_key()
            os.environ["GOOGLE_API_KEY"] = api_key
            return ModelClient._openai_compatible_model_client(
                api_key=api_key,
                model=model_config["model"],
                base_url=model_config["base_url"],
                timeout=30.0,
            )
        raise ValueError(f"Unsupported LLM provider: {provider}")

    @staticmethod
    def openai_model_client():
        return ModelClient.chat_model_client()

    @staticmethod
    def gemini_model_client():
        return ModelClient.chat_model_client()

    @staticmethod
    def _openai_compatible_model_client(
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ):
        kwargs = {
            "model": model,
            "model_info": ModelInfo(
                vision=True,
                function_calling=True,
                json_output=False,
                family="unknown",
                structured_output=False,
            ),
            "parallel_tool_calls": True,
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
            "temperature": 0.0,
            "top_p": 1.0,
            "default_headers": None,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return OpenAIChatCompletionClient(**kwargs)
