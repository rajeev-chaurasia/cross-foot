"""Capability filtered provider pools.

A live run over 105 documents lost 36 of them to one blind spot: the vision
chain walked every configured provider in priority order, and the second one
cannot read images at all. Its 400 is correctly classified as a bad request, so
it neither retries nor spills over, and each document died on the spot.

Everything here is offline and reads the capability table rather than naming
providers, so the table stays the single source of truth.
"""

from __future__ import annotations

import pytest

from crossfoot import cli
from crossfoot.config import NoProviderConfiguredError, Settings
from crossfoot.constants import (
    PROVIDER_CAPABILITIES,
    VISION_CAPABILITIES,
    Capability,
    Provider,
)

ENV_VARS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "CROSSFOOT_LLM_BASE_URL",
    "CROSSFOOT_LLM_API_KEY",
    "CROSSFOOT_LLM_MODEL",
)

KEYED_PROVIDERS: dict[Provider, str] = {
    Provider.GEMINI: "GEMINI_API_KEY",
    Provider.GROQ: "GROQ_API_KEY",
    Provider.OPENROUTER: "OPENROUTER_API_KEY",
    Provider.MISTRAL: "MISTRAL_API_KEY",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def _every_key(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The 2026-08-06 configuration: a key for every provider in the table."""
    return _settings(monkeypatch, **{name: "key" for name in KEYED_PROVIDERS.values()})


def _lacking(capability: Capability) -> Provider:
    """A keyed provider the table says cannot do this, so the test has a premise."""
    for provider in KEYED_PROVIDERS:
        if not PROVIDER_CAPABILITIES[provider].supports(capability):
            return provider
    pytest.skip(f"no configurable provider lacks {capability.value}")


def test_a_provider_without_vision_is_dropped_and_the_order_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _every_key(monkeypatch)
    _lacking(Capability.VISION)
    configured = [profile.name for profile in settings.configured_profiles()]

    pool = [profile.name for profile in settings.profile_pool(requires=(Capability.VISION,))]

    assert pool == [name for name in configured if PROVIDER_CAPABILITIES[name].supports_vision]
    assert len(pool) < len(configured)


def test_a_provider_without_structured_output_is_dropped_when_it_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _every_key(monkeypatch)
    _lacking(Capability.JSON_SCHEMA)
    configured = [profile.name for profile in settings.configured_profiles()]

    pool = [profile.name for profile in settings.profile_pool(requires=(Capability.JSON_SCHEMA,))]

    assert pool == [name for name in configured if PROVIDER_CAPABILITIES[name].supports_json_schema]
    assert len(pool) < len(configured)


def test_the_unfiltered_pool_still_means_every_configured_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Text only providers stay available for text only work.
    settings = _every_key(monkeypatch)
    assert settings.profile_pool() == settings.configured_profiles()


def test_an_empty_filtered_pool_names_the_missing_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _lacking(Capability.VISION)
    settings = _settings(monkeypatch, **{KEYED_PROVIDERS[provider]: "key"})

    with pytest.raises(NoProviderConfiguredError) as raised:
        settings.profile_pool(requires=(Capability.VISION,))

    assert Capability.VISION.value in str(raised.value)


def test_the_vision_chain_extract_builds_carries_no_text_only_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _every_key(monkeypatch)

    chain = cli.vision_profiles(settings)

    assert chain
    assert all(
        PROVIDER_CAPABILITIES[profile.name].supports(capability)
        for profile in chain
        for capability in VISION_CAPABILITIES
    )
    # A chain equal to the whole pool would pass the check above while proving
    # nothing, so the filter has to have removed something.
    assert len(chain) < len(settings.configured_profiles())
