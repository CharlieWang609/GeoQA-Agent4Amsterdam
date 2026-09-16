# SPDX-License-Identifier: GPL-3.0-only

"""The DeepSeek adapter carries the schema in the prompt, validates the
JSON-mode output locally, and repairs once."""

from __future__ import annotations

import json

import httpx
import pytest

from geoqa_agent.deepseek_client import DeepSeekChatClient
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactRequest,
    ProviderResponseError,
    SchemaConformanceError,
)

VALID_ANNOTATION = {"datasets": [], "layers": []}


def completion(content: object, *, finish_reason: str = "stop", model: str = "deepseek-flash") -> dict:
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": json.dumps(content), "reasoning_content": "..."},
            }
        ],
    }


def client_returning(payloads: list[dict], captured: list[dict]) -> DeepSeekChatClient:
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json=queue.pop(0))

    return DeepSeekChatClient(
        api_key="k",
        model="deepseek-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_request_carries_prompt_schema_and_thinking_and_reads_content():
    captured: list[dict] = []
    client = client_returning([completion(VALID_ANNOTATION)], captured)
    artifact = client.generate(
        ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}")
    )
    assert artifact.data == VALID_ANNOTATION
    assert artifact.provenance.provider == "deepseek"
    assert artifact.provenance.model == "deepseek-flash"
    body = captured[0]
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
    assert body["max_tokens"] == 65536
    system, user = body["messages"]
    assert system["role"] == "system" and "JSON Schema" in system["content"]
    assert system["content"].startswith("You are the GeoQA Data Semantic Annotation role")
    assert user == {"role": "user", "content": "{}"}


def test_nonconforming_output_gets_one_corrective_turn():
    captured: list[dict] = []
    client = client_returning([completion({"datasets": []}), completion(VALID_ANNOTATION)], captured)
    artifact = client.generate(
        ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}")
    )
    assert artifact.data == VALID_ANNOTATION
    assert len(captured) == 2
    repair = captured[1]["messages"]
    assert repair[2]["role"] == "assistant" and repair[3]["role"] == "user"
    assert "does not conform" in repair[3]["content"] and "layers" in repair[3]["content"]

    twice = client_returning([completion({"datasets": []}), completion({"datasets": []})], [])
    with pytest.raises(SchemaConformanceError):
        twice.generate(ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}"))


def test_truncation_and_model_mismatch_are_rejected():
    truncated = client_returning([completion(VALID_ANNOTATION, finish_reason="length")], [])
    with pytest.raises(ProviderResponseError):
        truncated.generate(ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}"))
    other = client_returning([completion(VALID_ANNOTATION, model="deepseek-v4-pro")], [])
    with pytest.raises(ProviderResponseError):
        other.generate(ArtifactRequest(contract=ArtifactContract.METADATA_ANNOTATION, input_text="{}"))
