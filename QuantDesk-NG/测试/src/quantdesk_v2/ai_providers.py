from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AiProviderPreset:
    """Server-owned AI endpoint metadata.

    Network callers must resolve destinations through this registry. Database
    rows and API payloads only select a provider code and can never override an
    origin or request path.
    """

    code: str
    label: str
    base_url: str
    host: str
    path: str
    default_model: str
    models: tuple[str, ...]


AI_PROVIDER_PRESETS: dict[str, AiProviderPreset] = {
    "openai": AiProviderPreset(
        code="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        host="api.openai.com",
        path="/v1/chat/completions",
        default_model="gpt-4.1-mini",
        models=("gpt-4.1-mini", "gpt-4.1", "o4-mini"),
    ),
    "deepseek": AiProviderPreset(
        code="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        host="api.deepseek.com",
        path="/chat/completions",
        default_model="deepseek-v4-flash",
        models=("deepseek-v4-flash", "deepseek-v4-pro"),
    ),
    "doubao": AiProviderPreset(
        code="doubao",
        label="豆包",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        host="ark.cn-beijing.volces.com",
        path="/api/v3/chat/completions",
        default_model="doubao-seed-2-0-lite-260215",
        models=("doubao-seed-2-0-lite-260215",),
    ),
    "qwen": AiProviderPreset(
        code="qwen",
        label="通义千问",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        host="dashscope.aliyuncs.com",
        path="/compatible-mode/v1/chat/completions",
        default_model="qwen3.7-plus",
        models=("qwen3.7-plus", "qwen3.6-plus", "qwen3.6-flash", "qwen-plus"),
    ),
    "kimi": AiProviderPreset(
        code="kimi",
        label="Kimi",
        base_url="https://api.moonshot.cn/v1",
        host="api.moonshot.cn",
        path="/v1/chat/completions",
        default_model="kimi-k3",
        models=("kimi-k3",),
    ),
    "minimax": AiProviderPreset(
        code="minimax",
        label="MiniMax",
        base_url="https://api.minimaxi.com/v1",
        host="api.minimaxi.com",
        path="/v1/chat/completions",
        default_model="MiniMax-M2.7",
        models=("MiniMax-M2.7", "MiniMax-M2.7-highspeed"),
    ),
}

SUPPORTED_AI_PROVIDERS = frozenset(AI_PROVIDER_PRESETS)


def get_ai_provider(code: str) -> AiProviderPreset | None:
    return AI_PROVIDER_PRESETS.get(code)
