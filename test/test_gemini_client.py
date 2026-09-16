# SPDX-License-Identifier: GPL-3.0-only

"""The Gemini Interactions adapter speaks the provider-neutral contract."""

from __future__ import annotations

import json

import httpx
import pytest

from geoqa_agent.gemini_client import GeminiInteractionsClient
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactRequest,
    ProviderResponseError,
)


def client_returning(payload: dict, captured: list[dict]) -> GeminiInteractionsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == "k"
        return httpx.Response(200, json=payload)

    return GeminiInteractionsClient(
        api_key="k",
        model="gemini-3.8-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_request_carries_the_role_contract_and_reads_output_text():
    captured: list[dict] = []
    client = client_returning(
        {
            "status": "completed",
            "model": "gemini-3.8-flash",
            "steps": [
                {"type": "thought", "signature": "x"},
                {"type": "model_output", "content": [{"type": "text", "text": json.dumps({"ok": 1})}]},
            ],
        },
        captured,
    )
    artifact = client.generate(
        ArtifactRequest(contract=ArtifactContract.WORKFLOW_PLANNING, input_text="{}")
    )
    assert artifact.data == {"ok": 1}
    assert artifact.provenance.provider == "google"
    assert artifact.provenance.model == "gemini-3.8-flash"
    body = captured[0]
    assert body["model"] == "gemini-3.8-flash"
    assert body["generation_config"] == {"thinking_level": "medium", "max_output_tokens": 32768}
    assert body["response_format"]["mime_type"] == "application/json"
    assert body["input"] == "{}"
    assert "uniqueItems" not in json.dumps(body["response_format"]["schema"])
    assert body["system_instruction"].startswith("You are the GeoQA Workflow Planning role")


def test_incomplete_interaction_and_model_mismatch_are_rejected():
    incomplete = client_returning({"status": "incomplete", "model": "gemini-3.8-flash", "steps": []}, [])
    with pytest.raises(ProviderResponseError):
        incomplete.generate(
            ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}")
        )

    other = client_returning(
        {
            "status": "completed",
            "model": "gemini-3.8-pro",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "{}"}]}],
        },
        [],
    )
    with pytest.raises(ProviderResponseError):
        other.generate(
            ArtifactRequest(contract=ArtifactContract.WORKFLOW_PLANNING, input_text="{}")
        )
