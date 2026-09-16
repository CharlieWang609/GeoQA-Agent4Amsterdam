# SPDX-License-Identifier: GPL-3.0-only

"""Build structured-artifact clients for the registered models from the
environment: one planning client per selectable model whose provider key
is configured, one annotation client for the annotation model."""

from __future__ import annotations

import os

from geoqa_agent.deepseek_client import DeepSeekChatClient
from geoqa_agent.gemini_client import GeminiInteractionsClient
from geoqa_agent.openai_client import OpenAIResponsesClient
from geoqa_agent.structured_artifacts import (
    ANNOTATION_MODEL,
    DEEPSEEK_PROVIDER,
    GOOGLE_PROVIDER,
    MODEL_CHOICES,
    ModelChoice,
    model_choice,
)

ProviderClient = OpenAIResponsesClient | GeminiInteractionsClient | DeepSeekChatClient

# The environment variable holding each provider's API key.
PROVIDER_KEYS = {
    GOOGLE_PROVIDER: "GOOGLE_API_KEY",
    DEEPSEEK_PROVIDER: "DEEPSEEK_API_KEY",
}


def structured_client(choice: ModelChoice) -> ProviderClient:
    if choice.provider == GOOGLE_PROVIDER:
        return GeminiInteractionsClient(
            api_key=os.environ["GOOGLE_API_KEY"], model=choice.model
        )
    if choice.provider == DEEPSEEK_PROVIDER:
        return DeepSeekChatClient(
            api_key=os.environ["DEEPSEEK_API_KEY"], model=choice.model
        )
    return OpenAIResponsesClient(
        api_key=os.environ["OPENAI_API_KEY"], model=choice.model
    )


def planning_clients() -> dict[str, ProviderClient]:
    """One client per registered model whose provider key is present; a
    model without a key is simply not offered."""

    return {
        choice.model: structured_client(choice)
        for choice in MODEL_CHOICES
        if os.environ.get(PROVIDER_KEYS.get(choice.provider, "OPENAI_API_KEY"))
    }


def annotation_client() -> ProviderClient:
    return structured_client(model_choice(ANNOTATION_MODEL))


def close_all(clients: dict[str, ProviderClient]) -> None:
    for client in clients.values():
        client.close()
