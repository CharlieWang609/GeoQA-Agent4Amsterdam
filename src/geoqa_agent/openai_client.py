# SPDX-License-Identifier: GPL-3.0-only

"""OpenAI Responses client for strict structured artifacts."""

from __future__ import annotations

import json
from typing import Mapping, cast

import httpx

from geoqa_agent.artifact_schemas import (
    CONFIGURATIONS,
    RoleConfiguration,
)
from geoqa_agent.structured_artifacts import (
    OPENAI_PROVIDER,
    OPENAI_RESPONSES_URL,
    ArtifactProvenance,
    ArtifactRequest,
    ProviderResponseError,
    SchemaConformanceError,
    StructuredArtifact,
)


def _provider_schema(schema: object) -> object:
    """OpenAI strict structured outputs reject "uniqueItems"."""

    if isinstance(schema, dict):
        return {
            key: _provider_schema(value)
            for key, value in schema.items()
            if key != "uniqueItems"
        }
    if isinstance(schema, list):
        return [_provider_schema(item) for item in schema]
    return schema


class OpenAIResponsesClient:
    """OpenAI Responses adapter behind the provider-neutral contract."""

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.Client | None = None,
        model: str,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._http_client = http_client or httpx.Client(timeout=60.0)
        self._owns_http_client = http_client is None
        self._model = model

    def close(self) -> None:
        """Close an internally managed HTTP client."""

        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> OpenAIResponsesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: ArtifactRequest) -> StructuredArtifact:
        configuration = self._configuration(request)
        settings = configuration.settings
        try:
            response = self._http_client.post(
                OPENAI_RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "instructions": configuration.instructions,
                    "input": request.input_text,
                    "reasoning": {"effort": settings.reasoning_effort},
                    "max_output_tokens": settings.max_output_tokens,
                    "store": False,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": configuration.schema_name,
                            "strict": True,
                            "schema": _provider_schema(configuration.schema),
                        }
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
        # Reject silent provider-side model substitution: provenance must
        # record the exact model that actually generated the artifact.
        response_model = payload.get("model")
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
                provider=OPENAI_PROVIDER,
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
    def _parse_output(payload: object) -> object:
        """Extract the first output_text part of a Responses API payload."""

        if not isinstance(payload, dict):
            raise ProviderResponseError("Provider response must be an object.")
        if payload.get("status") == "incomplete":
            details = payload.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            raise ProviderResponseError(
                f"Provider output is incomplete ({reason}); raise the role's "
                "max_output_tokens or shrink the input."
            )
        output = payload.get("output")
        if not isinstance(output, list):
            raise ProviderResponseError("Provider response has no output list.")
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    try:
                        return json.loads(part["text"])
                    except json.JSONDecodeError as error:
                        raise SchemaConformanceError(
                            "Provider output is not valid JSON."
                        ) from error
        raise ProviderResponseError(
            "Provider response contains no structured output text."
        )
