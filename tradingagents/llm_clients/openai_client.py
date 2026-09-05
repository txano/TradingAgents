import os
from typing import Any, Optional

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model

# Default request timeout for DeepSeek.  Reasoning models on large prompts
# (e.g. a 90+-ticker council synthesis, measured at ~87k input tokens) think
# for a long time before emitting their first byte, so streaming alone does
# not keep the socket busy — the read timeout has to cover the whole silent
# reasoning phase.  Raised from 600s after a 99-ticker run still died as
# "Connection error." at that ceiling.
DEEPSEEK_TIMEOUT_S = 1800

# Connect/write timeouts are a different problem from the read timeout above.
# The long read window exists so a model may think in silence; establishing a
# TCP connection has no such excuse, and inheriting 1800s there means a dropped
# network hangs a worker for half an hour per attempt (× DEEPSEEK_MAX_RETRIES)
# instead of failing fast. A 2026-08-12 run lost its Wi-Fi mid-flight and sat
# wedged with 8 workers at 0% CPU, the job neither finishing nor erroring.
DEEPSEEK_CONNECT_TIMEOUT_S = 20


# Read timeout for a *streaming* response.  httpx applies the read timeout to
# each individual read, so on a stream it is an inter-chunk gap, not a total
# budget — and DeepSeek keeps a streaming connection warm with ": keep-alive"
# SSE comments, so a healthy stream never goes minutes without a byte.  Applying
# the 1800s first-byte budget here instead meant a network that died mid-stream
# blocked a worker for 30 minutes per attempt; with SDK retries on top, a
# 2026-08-13 run sat wedged for 3.5 hours with all 8 workers in ssl.recv.
DEEPSEEK_STREAM_READ_TIMEOUT_S = 300


def _deepseek_timeout(streaming: bool = True) -> "httpx.Timeout":
    """Timeouts for one DeepSeek request.

    Three different clocks, deliberately:

    * **connect** — short. Establishing TCP has no reason to take longer, and a
      dead network should surface in seconds.
    * **read** — on a stream this is the gap *between chunks*, so keep-alives
      bound it to seconds; non-streaming has to cover the whole silent reasoning
      phase before the first byte, hence the much larger budget.
    * **write/pool** — generous; neither has ever been the failure mode.

    Used for both the shared httpx pool and the per-request timeout the openai
    SDK applies on top of it — set only one of the two and the other silently
    reinstates its own value everywhere.
    """
    read = DEEPSEEK_STREAM_READ_TIMEOUT_S if streaming else DEEPSEEK_TIMEOUT_S
    return httpx.Timeout(
        read,
        connect=DEEPSEEK_CONNECT_TIMEOUT_S,
        write=DEEPSEEK_TIMEOUT_S,
        pool=DEEPSEEK_TIMEOUT_S,
    )

# Connection-level retries per request.  The openai SDK defaults to 2, which a
# batch screen outlives: launching 8 workers at once fires a burst of graph
# construction and first LLM calls, and transport failures there outlasted all
# 3 attempts — 7 of the first 8 tickers died as "Connection error." while the
# run that followed was fine.  Retrying the individual call is far cheaper than
# losing a whole ticker's multi-agent run, so give the burst more headroom.
# The SDK backs off exponentially with jitter, so this costs nothing when calls
# succeed.  A user-supplied max_retries in config still wins (passthrough).
DEEPSEEK_MAX_RETRIES = 5

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
                # The openai SDK sets a per-request timeout that overrides this,
                # but httpx's own default is 5s — so anything that ever builds a
                # request without that explicit timeout would silently get 5s.
                # Long read window, short connect: see DEEPSEEK_CONNECT_TIMEOUT_S.
                timeout=_deepseek_timeout(),
            )
            _DEEPSEEK_HTTP_CLIENTS[pool_key] = client
    return client


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and capability-aware binding.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling.

    ``with_structured_output`` consults the per-model capability table
    (``capabilities.get_capabilities``) to pick the method and to decide
    whether ``tool_choice`` may be sent. Models that reject ``tool_choice``
    (e.g. DeepSeek V4 and reasoner — per their official tool-calling
    guide) still bind the schema as a tool, but no ``tool_choice``
    parameter is sent.

    Provider-specific quirks beyond structured-output (e.g. DeepSeek's
    reasoning_content roundtrip) live in subclasses so this base class
    stays small.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # When the model rejects tool_choice, suppress langchain's hardcoded
        # value. The schema is still bound as a tool — exactly what
        # DeepSeek's official tool-calling examples do.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Thinking-mode round-trip is the only DeepSeek-specific behavior that
    stays here. When DeepSeek's thinking models return a response with
    ``reasoning_content``, that field must be echoed back as part of the
    assistant message on the next turn or the API fails with HTTP 400.
    ``_create_chat_result`` captures it on receive and
    ``_get_request_payload`` re-attaches it on send.

    Tool-choice handling for V4 and reasoner — those models reject the
    ``tool_choice`` parameter — is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_)):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", [])
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax-specific overrides on top of the OpenAI-compatible client.

    M2.x reasoning models embed ``<think>...</think>`` blocks directly in
    ``message.content`` by default, which would pollute saved reports.
    Per platform.minimax.io/docs/api-reference/text-openai-api, setting
    ``reasoning_split=True`` in the request body redirects the thinking
    block into ``reasoning_details`` so ``content`` stays clean.

    Tool-choice handling for M2.x — those models accept only the string
    enum ``{"none", "auto"}`` and reject langchain's function-spec dict —
    is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload.setdefault("reasoning_split", True)
        return payload


# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "streaming",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs. API-key env vars live in api_key_env.PROVIDER_API_KEY_ENV
# (one canonical mapping consulted by both this client and the CLI's
# interactive key-prompt). Dual-region providers (qwen/glm/minimax) keep
# separate endpoints because international and China accounts cannot share
# credentials (#758).
_PROVIDER_BASE_URL = {
    "xai":        "https://api.x.ai/v1",
    "deepseek":   "https://api.deepseek.com",
    "qwen":       "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "qwen-cn":    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm":        "https://api.z.ai/api/paas/v4/",
    "glm-cn":     "https://open.bigmodel.cn/api/paas/v4/",
    "minimax":    "https://api.minimax.io/v1",
    "minimax-cn": "https://api.minimaxi.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama":     "http://localhost:11434/v1",
}


def _resolve_provider_base_url(provider: str) -> Optional[str]:
    """Default base URL for ``provider``, with env-var overrides where defined.

    Currently only Ollama supports an env-var override (``OLLAMA_BASE_URL``),
    matching the convention in the broader Ollama tooling ecosystem so users
    can point at a remote ollama-serve without editing code. The check is
    call-time, not import-time, so tests that monkeypatch the env after
    import behave correctly.
    """
    if provider == "ollama":
        env_url = os.environ.get("OLLAMA_BASE_URL")
        if env_url:
            return env_url
    return _PROVIDER_BASE_URL.get(provider)


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

        # Provider-specific base URL and auth. An explicit base_url on the
        # client (e.g. a corporate proxy) takes precedence over the
        # provider default so users can route through their own gateway.
        if self.provider in _PROVIDER_BASE_URL:
            llm_kwargs["base_url"] = self.base_url or _resolve_provider_base_url(self.provider)
            api_key_env = get_api_key_env(self.provider)
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
                else:
                    raise ValueError(
                        f"API key for provider '{self.provider}' is not set. "
                        f"Please set the {api_key_env} environment variable "
                        f"(e.g. add {api_key_env}=your_key to your .env file)."
                    )
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

        # DeepSeek: per-key httpx pool keeps parallel workers isolated.
        if self.provider == "deepseek":
            if llm_kwargs.get("max_retries") is None:
                llm_kwargs["max_retries"] = DEEPSEEK_MAX_RETRIES
            # Stream by default.  DeepSeek holds a slow request open by sending
            # bare empty lines on the non-streaming path but proper
            # ": keep-alive" SSE comments when streaming
            # (api-docs.deepseek.com/quick_start/rate_limit).  The empty-line
            # path does not survive long v4-pro generations: measured on an
            # identical prompt, no retries, 12 runs, v4-pro dropped 8/12 (67%)
            # of non-streaming requests mid-body and 0/12 when streaming.  This
            # is also why the council (which streams) kept working while the
            # agent pipeline (plain invoke) did not.  Free for v4-flash — same
            # 33.5s either way.  Set streaming=False in config to opt out.
            llm_kwargs.setdefault("streaming", True)
            # Set after `streaming` is resolved: the read timeout means a
            # different thing on a stream (inter-chunk gap) than off one
            # (first-byte budget). Reading it before the setdefault above always
            # picked the non-streaming ceiling.
            if "timeout" not in llm_kwargs:
                llm_kwargs["timeout"] = _deepseek_timeout(
                    streaming=bool(llm_kwargs.get("streaming", True))
                )
            if "http_client" not in llm_kwargs:
                llm_kwargs["http_client"] = _get_deepseek_http_client(
                    llm_kwargs.get("api_key", "")
                )

        # Provider-specific quirks live in their own subclasses.
        if self.provider == "deepseek":
            chat_cls = DeepSeekChatOpenAI
        elif self.provider in ("minimax", "minimax-cn"):
            chat_cls = MinimaxChatOpenAI
        else:
            chat_cls = NormalizedChatOpenAI
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
