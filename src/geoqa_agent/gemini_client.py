# SPDX-License-Identifier: GPL-3.0-only

"""Gemini Interactions client for strict structured artifacts."""

from __future__ import annotations

import json

import httpx

from geoqa_agent.artifact_schemas import CONFIGURATIONS, RoleConfiguration
from geoqa_agent.structured_artifacts import (
    GEMINI_INTERACTIONS_URL,
    GOOGLE_PROVIDER,
    ArtifactProvenance,
    ArtifactRequest,
    ProviderResponseError,
    SchemaConformanceError,
    StructuredArtifact,
)

# JSON Schema keywords the Gemini response_format schema does not accept.
_UNSUPPORTED_KEYWORDS = frozenset({"uniqueItems", "minLength"})


def _provider_schema(schema: object) -> object:
    if isinstance(schema, dict):
        return {
            key: _provider_schema(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_KEYWORDS
        }
    if isinstance(schema, list):
        return [_provider_schema(item) for item in schema]
    return schema


class GeminiInteractionsClient:
    """Gemini Interactions adapter behind the provider-neutral contract."""

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
        self._http_client = http_client or httpx.Client(timeout=120.0)
        self._owns_http_client = http_client is None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> GeminiInteractionsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: ArtifactRequest) -> StructuredArtifact:
        configuration = self._configuration(request)
        settings = configuration.settings
        try:
            response = self._http_client.post(
                GEMINI_INTERACTIONS_URL,
                headers={
                    "x-goog-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "system_instruction": configuration.instructions,
                    "input": request.input_text,
                    "store": False,
                    "generation_config": {
                        "thinking_level": settings.reasoning_effort,
                        "max_output_tokens": settings.max_output_tokens,
                    },
                    "response_format": {
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": _provider_schema(configuration.schema),
                    },
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
        response_model = payload.get("model", self._model)
        if response_model != self._model:
            raise ProviderResponseError(
                "Provider response model does not match the configured exact "
                f"model: expected {self._model!r}, got {response_model!r}."
            )
        data = self._parse_output(payload)
        if not isinstance(data, dict):
            raise SchemaConformanceError(
                f"{configuration.schema_name} output must be a JSON object."
            )
        return StructuredArtifact(
            data=data,
            provenance=ArtifactProvenance(
                provider=GOOGLE_PROVIDER,
                model=response_model,
                role=configuration.role,
                settings=settings,
                prompt_version=configuration.prompt_version,
                schema_version=configuration.schema_version,
            ),
        )

    @staticmethod
    def _configuration(request: ArtifactRequest) -> RoleConfiguration:
        return CONFIGURATIONS[request.contract]

    @staticmethod
    def _parse_output(payload: dict[str, object]) -> object:
        """The text of the interaction's model_output step.

        A completed interaction lists its steps (thoughts, then one
        model_output whose content parts carry the JSON text).
        """

        if payload.get("status") != "completed":
            details = {
                key: value
                for key, value in payload.items()
                if key not in {"steps", "object", "created", "updated"}
            }
            raise ProviderResponseError(
                f"Provider interaction did not complete: {json.dumps(details)[:800]}"
            )
        steps = payload.get("steps")
        if not isinstance(steps, list):
            raise ProviderResponseError("Provider response has no steps.")
        texts = [
            part["text"]
            for step in steps
            if isinstance(step, dict) and step.get("type") == "model_output"
            for part in step.get("content", [])
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if not texts:
            raise ProviderResponseError(
                "Provider response contains no structured output text."
            )
        try:
            return json.loads("".join(texts))
        except json.JSONDecodeError as error:
            raise SchemaConformanceError("Provider output is not valid JSON.") from error
