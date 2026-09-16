# SPDX-License-Identifier: GPL-3.0-only

"""FastAPI routes for the Live Sandbox Question Session lifecycle."""

from __future__ import annotations

from asyncio import to_thread
import base64
from collections.abc import Callable
from datetime import UTC, datetime
import json
import mimetypes
from pathlib import Path
import re
from typing import Annotated, Literal, Mapping
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, field_validator, StringConstraints

from app.api.answer_maps import AnswerMapUnavailableError
from app.api.raster_views import (
    ObservationNotFoundError,
    RasterViewUnavailableError,
    build_catalog_raster_view,
)
from app.api.catalog_layers import (
    MAX_PREVIEW_FEATURES,
    PREVIEW_CACHE_CONTROL,
    CatalogLayerNotFoundError,
    CatalogLayerPreviewUnavailableError,
    build_catalog_layer_listing,
    build_catalog_layer_preview,
    find_catalog_layer,
)
from app.api.question_sessions import QuestionSessionService
from app.api.session_repository import (
    SessionExpiredError,
    SessionNotFoundError,
    SessionStateTransitionError,
)
from app.api.session_models import QuestionSession
from app.api.showcase import (
    ShowcaseNotFoundError,
    list_showcase,
    read_showcase_document,
)
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import ExecutionWorker
from geoqa_agent.structured_artifacts import (
    MODEL_CHOICES,
    DEFAULT_MODEL,
    StructuredArtifactClient,
)
from geoqa_agent.tool_registry import ToolRegistry


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
SPA_CACHE_CONTROL = "no-cache"
# Worked examples change only when an operator republishes one.
SHOWCASE_CACHE_CONTROL = "public, max-age=300"
HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
VITE_HASHED_ASSET_PATTERN = re.compile(
    r".+-[A-Za-z0-9_-]{8}(?:\.[^.]+)+$"
)


class CreateQuestionSessionRequest(BaseModel):
    """The only public submission payload accepted by the Live Sandbox."""

    model_config = ConfigDict(extra="forbid")

    question: NonBlankText
    model: str = DEFAULT_MODEL

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in {choice.model for choice in MODEL_CHOICES}:
            raise ValueError(f"Unknown model: {value}")
        return value


class EditQuestionSessionRequest(BaseModel):
    """Free-text human review instruction, never a direct plan mutation."""

    model_config = ConfigDict(extra="forbid")

    instruction: NonBlankText


class RegenerateQuestionSessionRequest(BaseModel):
    """An intentionally empty regeneration command."""

    model_config = ConfigDict(extra="forbid")


class ResultDecisionRequest(BaseModel):
    """The owning Review Actor's post-execution decision."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["accepted", "rejected"]
    feedback: NonBlankText | None = None


class CurrentIdentity(BaseModel):
    """The authenticated caller identity exposed to the browser."""

    principal_id: str
    display_name: str


def create_question_session_app(
    *,
    storage: ObjectStore,
    structured_clients: Mapping[str, StructuredArtifactClient],
    tool_registry: ToolRegistry,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    session_id_factory: Callable[[], str] = lambda: str(uuid4()),
    execution_worker: ExecutionWorker,
    job_id_factory: Callable[[], str] = lambda: str(uuid4()),
    static_directory: Path | None = None,
) -> FastAPI:
    """Build the Live Sandbox API with explicit platform-owned dependencies."""

    service = QuestionSessionService(
        storage=storage,
        structured_clients=structured_clients,
        tool_registry=tool_registry,
        clock=clock,
        session_id_factory=session_id_factory,
        execution_worker=execution_worker,
        job_id_factory=job_id_factory,
    )

    app = FastAPI(title="GeoQA Agent Live Sandbox")

    # One translation table instead of per-route try/except blocks: every
    # domain error raised inside a route resolves to its HTTP status here
    # (FastAPI walks the exception MRO, so subclasses take precedence).
    def _register(
        exception: type[Exception],
        status_code: int,
        *,
        with_detail: bool = True,
    ) -> None:
        async def handler(request: Request, error: Exception) -> JSONResponse:
            del request
            detail = str(error) if with_detail else None
            return JSONResponse(
                status_code=status_code,
                content={"detail": detail},
            )

        app.add_exception_handler(exception, handler)

    _register(SessionNotFoundError, status.HTTP_404_NOT_FOUND, with_detail=False)
    _register(CatalogLayerNotFoundError, status.HTTP_404_NOT_FOUND, with_detail=False)
    _register(SessionExpiredError, status.HTTP_410_GONE, with_detail=False)
    _register(SessionStateTransitionError, status.HTTP_409_CONFLICT)
    _register(AnswerMapUnavailableError, status.HTTP_409_CONFLICT)
    _register(ObservationNotFoundError, status.HTTP_404_NOT_FOUND, with_detail=False)
    _register(ShowcaseNotFoundError, status.HTTP_404_NOT_FOUND, with_detail=False)
    _register(RasterViewUnavailableError, status.HTTP_409_CONFLICT)
    _register(
        CatalogLayerPreviewUnavailableError,
        status.HTTP_409_CONFLICT,
        with_detail=False,
    )
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Identity comes from Azure Container Apps Easy Auth headers; the
    # platform injects them after GitHub login and strips spoofed inbound
    # copies, so their presence is trusted here.
    async def principal_id(
        value: str | None = Header(
            default=None,
            alias="X-MS-CLIENT-PRINCIPAL-ID",
        ),
    ) -> str:
        if value is None or not value.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="GitHub authentication is required.",
            )
        return value.strip()

    @app.get("/api/me")
    async def current_identity(
        owner: str = Depends(principal_id),
        encoded_principal: str | None = Header(
            default=None,
            alias="X-MS-CLIENT-PRINCIPAL",
        ),
    ) -> CurrentIdentity:
        return CurrentIdentity(
            principal_id=owner,
            display_name=_principal_display_name(encoded_principal, owner),
        )

    @app.get("/api/question-sessions")
    async def list_sessions(
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        sessions = await to_thread(service.list, owner_principal_id=owner)
        return JSONResponse(
            content=[session.model_dump(mode="json") for session in sessions]
        )

    @app.get("/api/catalog-layers")
    async def list_catalog_layers(
        if_none_match: str | None = Header(
            default=None,
            alias="If-None-Match",
        ),
    ) -> Response:
        listing = build_catalog_layer_listing(storage)
        catalog_version = listing["catalog_version"]
        headers = (
            {}
            if catalog_version is None
            else {"ETag": f'"{catalog_version}"'}
        )
        if headers and _etag_matches(if_none_match, headers["ETag"]):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers=headers,
            )
        return JSONResponse(content=listing, headers=headers)

    @app.get(
        "/api/catalog-layers/{dataset}/{feature_type}/preview"
    )
    async def preview_catalog_layer(
        dataset: str,
        feature_type: str,
        if_none_match: str | None = Header(
            default=None,
            alias="If-None-Match",
        ),
    ) -> Response:
        layer = find_catalog_layer(
            storage,
            dataset=dataset,
            feature_type=feature_type,
        )
        if layer.vector is None or layer.vector.feature_count > MAX_PREVIEW_FEATURES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    "raster layers have no preview"
                    if layer.vector is None
                    else "layer too large to preview"
                ),
            )
        headers = {
            "ETag": f'"{layer.content_hash}"',
            "Cache-Control": PREVIEW_CACHE_CONTROL,
        }
        if _etag_matches(if_none_match, headers["ETag"]):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers=headers,
            )
        preview = build_catalog_layer_preview(storage, layer)
        return JSONResponse(content=preview, headers=headers)

    @app.get("/api/catalog-layers/{dataset}/{feature_type}/raster-view")
    async def catalog_raster_view(dataset: str, feature_type: str) -> JSONResponse:
        """The rendered raster or the latest-window satellite mosaic of one
        Catalog layer, as an image overlay with its legend."""

        layer = find_catalog_layer(
            storage,
            dataset=dataset,
            feature_type=feature_type,
        )
        view = await to_thread(build_catalog_raster_view, storage, layer)
        return JSONResponse(content=view)

    # Worked examples: frozen documents of accepted sessions, readable
    # without signing in (see app.api.showcase).
    @app.get("/api/showcase")
    async def list_worked_examples() -> JSONResponse:
        examples = await to_thread(list_showcase, storage)
        return JSONResponse(
            content=[example.model_dump(mode="json") for example in examples],
            headers={"Cache-Control": SHOWCASE_CACHE_CONTROL},
        )

    @app.get("/api/showcase/{session_id}")
    async def get_worked_example(session_id: str) -> JSONResponse:
        return await _showcase_document(session_id, "session.json")

    @app.get("/api/showcase/{session_id}/answer-map")
    async def get_worked_example_answer_map(session_id: str) -> JSONResponse:
        return await _showcase_document(session_id, "answer-map.json")

    @app.get("/api/showcase/{session_id}/observations")
    async def list_worked_example_observations(session_id: str) -> JSONResponse:
        return await _showcase_document(session_id, "observations.json")

    @app.get("/api/showcase/{session_id}/observations/{ref}")
    async def get_worked_example_observation(session_id: str, ref: str) -> JSONResponse:
        return await _showcase_document(session_id, f"observations/{ref}.json")

    async def _showcase_document(session_id: str, name: str) -> JSONResponse:
        document = await to_thread(read_showcase_document, storage, session_id, name)
        return JSONResponse(
            content=document,
            headers={"Cache-Control": SHOWCASE_CACHE_CONTROL},
        )

    @app.post("/api/question-sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: CreateQuestionSessionRequest,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session = await to_thread(
            service.create,
            owner_principal_id=owner,
            question=request.question,
            model=request.model,
        )
        return _session_response(session, status.HTTP_201_CREATED)

    @app.get("/api/models")
    async def list_models() -> JSONResponse:
        return JSONResponse(
            content={
                "default": next(iter(structured_clients)),
                "models": [
                    {"model": choice.model, "label": choice.label, "provider": choice.provider}
                    for choice in MODEL_CHOICES
                    if choice.model in structured_clients
                ],
            }
        )

    @app.get("/api/question-sessions/{session_id}")
    async def get_session(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session = await to_thread(service.get, session_id, owner_principal_id=owner)
        return _session_response(session)

    @app.delete(
        "/api/question-sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> Response:
        """Delete session documents only, leaving expiry-governed artifacts
        to their standing 7-day expiry."""

        await to_thread(service.delete, session_id, owner_principal_id=owner)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/question-sessions/{session_id}/answer-map")
    async def get_answer_map(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        answer_map = await to_thread(
            service.get_answer_map,
            session_id,
            owner_principal_id=owner,
        )
        return JSONResponse(content=answer_map)

    @app.get("/api/question-sessions/{session_id}/observations")
    async def list_observations(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        listing = await to_thread(
            service.get_observations,
            session_id,
            owner_principal_id=owner,
        )
        return JSONResponse(content=listing)

    @app.get("/api/question-sessions/{session_id}/observations/{ref}")
    async def get_observation_view(
        session_id: str,
        ref: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        view = await to_thread(
            service.get_observation_view,
            session_id,
            ref,
            owner_principal_id=owner,
        )
        return JSONResponse(content=view)

    @app.post("/api/question-sessions/{session_id}/edit")
    async def edit_session(
        session_id: str,
        request: EditQuestionSessionRequest,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session = await to_thread(
            service.edit,
            session_id,
            owner_principal_id=owner,
            instruction=request.instruction,
        )
        return _session_response(session)

    @app.post("/api/question-sessions/{session_id}/regenerate")
    async def regenerate_session(
        session_id: str,
        request: RegenerateQuestionSessionRequest | None = None,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        del request
        session = await to_thread(
            service.regenerate, session_id, owner_principal_id=owner
        )
        return _session_response(session)

    @app.post("/api/question-sessions/{session_id}/result-decision")
    async def decide_result(
        session_id: str,
        request: ResultDecisionRequest,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session = await to_thread(
            service.decide_result,
            session_id,
            owner_principal_id=owner,
            decision=request.decision,
            feedback=request.feedback,
        )
        return _session_response(session)

    if static_directory is not None:
        root = static_directory.resolve()
        index = root / "index.html"
        if not index.is_file():
            raise ValueError(
                f"Static web build does not contain index.html: {static_directory}"
            )
        index_content = index.read_bytes()

        # SPA serving: real files are returned directly (hashed Vite assets
        # as immutable, other assets no-cache); any other path falls back to
        # index.html so client-side routing works on deep links.
        @app.get("/{web_path:path}", include_in_schema=False)
        async def serve_web_application(web_path: str) -> Response:
            if web_path == "api" or web_path.startswith("api/"):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            requested = (root / web_path).resolve()
            if requested.is_relative_to(root) and requested.is_file():
                media_type, _ = mimetypes.guess_type(requested.name)
                relative_path = requested.relative_to(root)
                is_hashed_asset = (
                    relative_path.parts[0] == "assets"
                    and VITE_HASHED_ASSET_PATTERN.fullmatch(requested.name)
                    is not None
                )
                headers = (
                    {"Cache-Control": HASHED_ASSET_CACHE_CONTROL}
                    if is_hashed_asset
                    else {"Cache-Control": SPA_CACHE_CONTROL}
                    if requested == index or relative_path.parts[0] == "assets"
                    else None
                )
                return Response(
                    content=requested.read_bytes(),
                    media_type=media_type,
                    headers=headers,
                )
            return Response(
                content=index_content,
                media_type="text/html",
                headers={"Cache-Control": SPA_CACHE_CONTROL},
            )

    return app


def _principal_display_name(
    encoded_principal: str | None,
    fallback: str,
) -> str:
    """Extract the display-name claim from the base64 X-MS-CLIENT-PRINCIPAL
    document Easy Auth injects; fall back to the principal id."""

    if encoded_principal is None:
        return fallback
    payload = json.loads(base64.b64decode(encoded_principal))
    for claim in payload.get("claims", []):
        if claim.get("typ") == payload.get("name_typ"):
            return str(claim["val"])
    return fallback


def _session_response(
    session: QuestionSession,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    return JSONResponse(
        content=session.model_dump(mode="json"),
        status_code=status_code,
    )


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    return any(
        candidate == "*" or candidate.removeprefix("W/") == etag
        for candidate in (
            item.strip() for item in if_none_match.split(",")
        )
    )
