# SPDX-License-Identifier: GPL-3.0-only

"""DeepSeek chat-completions client for structured artifacts.

DeepSeek's JSON mode guarantees valid JSON but not a schema, so the schema
travels in the system prompt and the output is validated locally; one
corrective turn is allowed before the artifact is rejected.
"""

from __future__ import annotations

import json
from typing import Mapping

import httpx
import jsonschema

from geoqa_agent.artifact_schemas import CONFIGURATIONS, RoleConfiguration
from geoqa_agent.structured_artifacts import (
    DEEPSEEK_CHAT_URL,
    DEEPSEEK_PROVIDER,
    ArtifactProvenance,
    ArtifactRequest,
    ProviderResponseError,
    SchemaConformanceError,
    StructuredArtifact,
)

# The role's reasoning effort in DeepSeek's thinking-mode vocabulary.
_EFFORT = {"none": "none", "low": "low", "medium": "high", "high": "high", "xhigh": "max"}


class DeepSeekChatClient:
    """DeepSeek chat-completions adapter behind the provider-neutral contract."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._model = model
        self._http_client = http_client or httpx.Client(timeout=300.0)
        self._owns_http_client = http_client is None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> DeepSeekChatClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: ArtifactRequest) -> StructuredArtifact:
        configuration = self._configuration(request)
        settings = configuration.settings
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"{configuration.instructions}\n\nRespond with exactly one JSON "
                    "object and nothing else. It must conform to this JSON Schema:\n"
                    f"{json.dumps(configuration.schema)}"
                ),
            },
            {"role": "user", "content": request.input_text},
        ]
        data = self._complete(messages, settings.reasoning_effort, settings.max_output_tokens)
        try:
            jsonschema.validate(data, configuration.schema)
        except jsonschema.ValidationError as error:
            # One corrective turn: JSON mode does not enforce the schema.
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That JSON does not conform to the schema: "
                        f"{error.message} (at {'/'.join(str(p) for p in error.absolute_path) or 'root'}). "
                        "Return the corrected complete JSON object."
                    ),
                }
            )
            data = self._complete(messages, settings.reasoning_effort, settings.max_output_tokens)
            try:
                jsonschema.validate(data, configuration.schema)
            except jsonschema.ValidationError as again:
                raise SchemaConformanceError(
                    f"{configuration.schema_name} output does not conform: {again.message}"
                ) from again
        return StructuredArtifact(
            data=data,
            provenance=ArtifactProvenance(
                provider=DEEPSEEK_PROVIDER,
                model=self._model,
                role=configuration.role,
                settings=settings,
                prompt_version=configuration.prompt_version,
                schema_version=configuration.schema_version,
            ),
        )

    def _complete(
        self,
        messages: list[dict[str, str]],
        reasoning_effort: str,
        max_output_tokens: int,
    ) -> Mapping[str, object]:
        effort = _EFFORT[reasoning_effort]
        try:
            response = self._http_client.post(
                DEEPSEEK_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                    "max_tokens": max_output_tokens,
                    "thinking": (
                        {"type": "disabled"}
                        if effort == "none"
                        else {"type": "enabled", "reasoning_effort": effort}
                    ),
                    "stream": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise ProviderResponseError(
                "Structured-artifact provider request failed: "
                f"{error.response.status_code} {error.response.text[:500]}"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderResponseError(
                "Structured-artifact provider request failed."
            ) from error
        if not isinstance(payload, dict):
            raise ProviderResponseError("Provider response must be an object.")
        response_model = payload.get("model")
        if response_model != self._model:
            raise ProviderResponseError(
                "Provider response model does not match the configured exact "
                f"model: expected {self._model!r}, got {response_model!r}."
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderResponseError("Provider response has no choices.")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise ProviderResponseError(
                f"Provider output ended with {choice.get('finish_reason')!r}; raise "
                "the role's max_output_tokens or shrink the input."
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("Provider response contains no output text.")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise SchemaConformanceError("Provider output is not valid JSON.") from error
        if not isinstance(data, dict):
            raise SchemaConformanceError("Provider output must be a JSON object.")
        return data

    @staticmethod
    def _configuration(request: ArtifactRequest) -> RoleConfiguration:
        return CONFIGURATIONS[request.contract]
