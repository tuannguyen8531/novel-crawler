"""LLM provider factory — creates and caches the global LLM instance."""

from src.config import config
from src.services.llm.base import BaseProvider
from src.services.llm.gemini import GeminiProvider
from src.services.llm.ollama import OllamaProvider

_PROVIDER_MAP: dict[str, type[BaseProvider]] = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
}

_llm_instance: BaseProvider | None = None


def _create_provider(name: str) -> BaseProvider:
    """Create a provider instance by name."""
    provider_cls = _PROVIDER_MAP.get(name)
    if provider_cls is None:
        raise ValueError(f"Unknown LLM provider: {name}")
    return provider_cls()


def get_llm() -> BaseProvider:
    """Get the global LLM service instance, with fallback if configured."""
    global _llm_instance
    if _llm_instance is None:
        primary = _create_provider(config.llm_provider)

        _llm_instance = primary

    return _llm_instance


def reset_llm():
    """Reset the global LLM instance (useful after config changes)."""
    global _llm_instance
    if _llm_instance is not None:
        _llm_instance.close()
    _llm_instance = None
