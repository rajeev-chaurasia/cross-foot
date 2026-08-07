"""Runtime configuration, loaded from environment variables and .env.

Provider keys use each provider's conventional variable name (GEMINI_API_KEY
and friends) so one .env serves this project and any gateway pointed at it.
Crossfoot-specific settings carry the CROSSFOOT_ prefix.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from crossfoot.constants import (
    PROVIDER_BASE_URLS,
    PROVIDER_CAPABILITIES,
    PROVIDER_DEFAULT_MODELS,
    PROVIDER_PRIORITY,
    Capability,
    Provider,
)

NO_PROVIDER_MESSAGE = "No provider key found. Copy .env.example to .env and add at least one key."
NO_CAPABLE_PROVIDER_TEMPLATE = (
    "No configured provider supports {capabilities}."
    " Add a key for a provider that does; see PROVIDER_CAPABILITIES."
)


class NoProviderConfiguredError(RuntimeError):
    """Raised when no provider key and no custom gateway is configured."""


class ProviderProfile(BaseModel):
    name: Provider
    base_url: str
    api_key: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    groq_api_key: str = Field(default="", validation_alias="GROQ_API_KEY")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    mistral_api_key: str = Field(default="", validation_alias="MISTRAL_API_KEY")

    # Optional custom OpenAI-compatible gateway, e.g. a local Penstock.
    # When set it becomes the first profile in the pool.
    custom_base_url: str = Field(default="", validation_alias="CROSSFOOT_LLM_BASE_URL")
    custom_api_key: str = Field(default="", validation_alias="CROSSFOOT_LLM_API_KEY")
    custom_model: str = Field(default="", validation_alias="CROSSFOOT_LLM_MODEL")

    llm_timeout_seconds: float = Field(
        default=120.0, validation_alias="CROSSFOOT_LLM_TIMEOUT_SECONDS"
    )
    data_dir: Path = Field(default=Path("data"), validation_alias="CROSSFOOT_DATA_DIR")

    def _provider_key(self, provider: Provider) -> str:
        keys: dict[Provider, str] = {
            Provider.GEMINI: self.gemini_api_key,
            Provider.GROQ: self.groq_api_key,
            Provider.OPENROUTER: self.openrouter_api_key,
            Provider.MISTRAL: self.mistral_api_key,
        }
        return keys[provider]

    def configured_profiles(self) -> list[ProviderProfile]:
        """Profiles in call-priority order; the custom gateway, when set, comes first."""
        profiles: list[ProviderProfile] = []
        if self.custom_base_url:
            profiles.append(
                ProviderProfile(
                    name=Provider.CUSTOM,
                    base_url=self.custom_base_url,
                    api_key=self.custom_api_key,
                    model=self.custom_model or PROVIDER_DEFAULT_MODELS[Provider.GEMINI],
                )
            )
        profiles.extend(
            ProviderProfile(
                name=provider,
                base_url=PROVIDER_BASE_URLS[provider],
                api_key=key,
                model=PROVIDER_DEFAULT_MODELS[provider],
            )
            for provider in PROVIDER_PRIORITY
            if (key := self._provider_key(provider))
        )
        return profiles

    def profile_pool(self, *, requires: Sequence[Capability] = ()) -> list[ProviderProfile]:
        """Configured profiles in priority order, for the spillover pool.

        With `requires` the pool keeps only providers that can serve the call.
        A capability blind pool is what killed 36 documents on 2026-08-06: Groq
        cannot read images, and its 400 neither retries nor spills over.
        """
        configured = self.configured_profiles()
        pool = [
            profile
            for profile in configured
            if all(
                PROVIDER_CAPABILITIES[profile.name].supports(capability) for capability in requires
            )
        ]
        if not pool:
            raise _no_pool_error(configured, requires)
        return pool

    def primary_profile(self) -> ProviderProfile:
        return self.profile_pool()[0]


def _no_pool_error(
    configured: Sequence[ProviderProfile], requires: Sequence[Capability]
) -> NoProviderConfiguredError:
    """Say which capability emptied the pool, not just that it is empty."""
    if not configured:
        return NoProviderConfiguredError(NO_PROVIDER_MESSAGE)
    # A capability no configured provider has at all names the real gap. When
    # each one is covered separately but no single provider covers them
    # together, the whole requirement is the gap.
    missing = [
        capability
        for capability in requires
        if not any(
            PROVIDER_CAPABILITIES[profile.name].supports(capability) for profile in configured
        )
    ] or list(requires)
    named = ", ".join(capability.value for capability in missing)
    return NoProviderConfiguredError(NO_CAPABLE_PROVIDER_TEMPLATE.format(capabilities=named))
