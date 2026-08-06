import pytest

from crossfoot.config import NoProviderConfiguredError, Settings
from crossfoot.constants import Provider

ENV_VARS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "CEREBRAS_API_KEY",
    "CROSSFOOT_LLM_BASE_URL",
    "CROSSFOOT_LLM_API_KEY",
    "CROSSFOOT_LLM_MODEL",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_no_keys_means_no_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).configured_profiles() == []


def test_primary_without_keys_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NoProviderConfiguredError):
        _settings(monkeypatch).primary_profile()


def test_profiles_follow_priority_order(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, GROQ_API_KEY="g", GEMINI_API_KEY="k", MISTRAL_API_KEY="m")
    names = [profile.name for profile in settings.configured_profiles()]
    assert names == [Provider.GEMINI, Provider.GROQ, Provider.MISTRAL]


def test_custom_gateway_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        GEMINI_API_KEY="k",
        CROSSFOOT_LLM_BASE_URL="http://localhost:8080/v1",
        CROSSFOOT_LLM_MODEL="auto",
    )
    profiles = settings.configured_profiles()
    assert profiles[0].name == Provider.CUSTOM
    assert profiles[0].model == "auto"
    assert profiles[1].name == Provider.GEMINI


def test_profile_carries_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _settings(monkeypatch, GEMINI_API_KEY="secret").primary_profile()
    assert profile.api_key == "secret"
    assert profile.base_url.startswith("https://generativelanguage.googleapis.com")
