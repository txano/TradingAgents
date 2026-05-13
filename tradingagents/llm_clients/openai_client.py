import os
from typing import Any, Optional

import httpx
from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

# Per-api-key httpx clients for DeepSeek.  One pool per key so that parallel
# workers using different keys don't share connections.  Each pool is sized
# generously enough to support several concurrent in-flight requests per key.
_DEEPSEEK_HTTP_CLIENTS: dict[str, httpx.Client] = {}
_DEEPSEEK_CLIENTS_LOCK = __import__("threading").Lock()


def _get_deepseek_http_client(api_key: str = "") -> httpx.Client:
    pool_key = api_key or "__default__"
    with _DEEPSEEK_CLIENTS_LOCK:
        client = _DEEPSEEK_HTTP_CLIENTS.get(pool_key)
        if client is None or client.is_closed:
            client = httpx.Client(
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
            _DEEPSEEK_HTTP_CLIENTS[pool_key] = client
    return client


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). This normalizes to string for consistent
    downstream handling.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs and API key env vars
_PROVIDER_CONFIG = {
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "glm": ("https://api.z.ai/api/paas/v4/", "ZHIPU_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        # Provider-specific base URL and auth
        if self.provider in _PROVIDER_CONFIG:
            base_url, api_key_env = _PROVIDER_CONFIG[self.provider]
            llm_kwargs["base_url"] = base_url
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        # DeepSeek thinking mode requires reasoning_content to be echoed back
        # in every subsequent message. LangChain strips it, breaking multi-turn
        # debate rounds. Disable via extra_body so the field goes into the HTTP
        # request body rather than as a keyword arg to completions.create().
        if self.provider == "deepseek":
            llm_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            if "timeout" not in llm_kwargs:
                llm_kwargs["timeout"] = 120
            if "http_client" not in llm_kwargs:
                llm_kwargs["http_client"] = _get_deepseek_http_client(
                    llm_kwargs.get("api_key", "")
                )

        return NormalizedChatOpenAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
