# SPDX-License-Identifier: GPL-3.0-only

"""FastAPI routes for the Live Sandbox Question Session lifecycle."""

from __future__ import annotations

from asyncio import to_thread
import base64
import binascii
from collections.abc import Callable
from datetime import UTC, datetime
import json
import mimetypes
from pathlib import Path
import re
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.api.answer_maps import AnswerMapUnavailableError
from app.api.catalog_layers import (
    MAX_PREVIEW_FEATURES,
    PREVIEW_CACHE_CONTROL,
    CatalogLayerNotFoundError,
    CatalogLayerPreviewUnavailableError,
    build_catalog_layer_listing,
    build_catalog_layer_preview,
    find_catalog_layer,
)
from app.api.question_sessions import (
    QuestionSessionService,
    SessionExpiredError,
    SessionNotFoundError,
    SessionPreconditionError,
    SessionPreconditionRequiredError,
    SessionStateTransitionError,
    normalize_session_etag,
)
from app.api.session_models import QuestionSession
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import (
    ExecutionAuthorizationError,
    ExecutionJob,
    ExecutionWorker,
)
from geoqa_agent.structured_artifacts import StructuredArtifactClient
from geoqa_agent.tool_registry import ToolRegistry


NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
SPA_CACHE_CONTROL = "no-cache"
HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
VITE_HASHED_ASSET_PATTERN = re.compile(
    r".+-[A-Za-z0-9_-]{8}(?:\.[^.]+)+$"
)


class CreateQuestionSessionRequest(BaseModel):
    """The only public submission payload accepted by the Live Sandbox."""

    model_config = ConfigDict(extra="forbid")

    question: NonBlankText


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
    structured_client: StructuredArtifactClient,
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
        structured_client=structured_client,
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
    _register(SessionPreconditionRequiredError, status.HTTP_428_PRECONDITION_REQUIRED)
    _register(SessionPreconditionError, status.HTTP_412_PRECONDITION_FAILED)
    _register(SessionStateTransitionError, status.HTTP_409_CONFLICT)
    _register(ExecutionAuthorizationError, status.HTTP_409_CONFLICT)
    _register(AnswerMapUnavailableError, status.HTTP_409_CONFLICT)
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
        sessions = service.list(owner_principal_id=owner)
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
        if layer.vector.feature_count > MAX_PREVIEW_FEATURES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="layer too large to preview",
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

    @app.post("/api/question-sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: CreateQuestionSessionRequest,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session, etag = service.create(
            owner_principal_id=owner,
            question=request.question,
        )
        return _session_response(session, etag, status.HTTP_201_CREATED)

    @app.get("/api/question-sessions/{session_id}")
    async def get_session(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        session, etag = service.get(session_id, owner_principal_id=owner)
        return _session_response(session, etag)

    @app.delete(
        "/api/question-sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session(
        session_id: str,
        owner: str = Depends(principal_id),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> Response:
        """Delete session documents only, leaving expiry-governed artifacts
        to their standing 7-day expiry."""

        service.delete(
            session_id,
            owner_principal_id=owner,
            if_match=if_match,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/question-sessions/{session_id}/execution-job")
    async def get_execution_job(
        session_id: str,
        owner: str = Depends(principal_id),
    ) -> JSONResponse:
        job = service.get_execution_job(
            session_id,
            owner_principal_id=owner,
        )
        return _execution_job_response(job)

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

    @app.post("/api/question-sessions/{session_id}/edit")
    async def edit_session(
        session_id: str,
        request: EditQuestionSessionRequest,
        owner: str = Depends(principal_id),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> JSONResponse:
        session, etag = service.edit(
            session_id,
            owner_principal_id=owner,
            instruction=request.instruction,
            expected_etag=normalize_session_etag(if_match),
        )
        return _session_response(session, etag)

    @app.post("/api/question-sessions/{session_id}/regenerate")
    async def regenerate_session(
        session_id: str,
        request: RegenerateQuestionSessionRequest | None = None,
        owner: str = Depends(principal_id),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> JSONResponse:
        del request
        session, etag = service.regenerate(
            session_id,
            owner_principal_id=owner,
            expected_etag=normalize_session_etag(if_match),
        )
        return _session_response(session, etag)

    @app.post("/api/question-sessions/{session_id}/result-decision")
    async def decide_result(
        session_id: str,
        request: ResultDecisionRequest,
        owner: str = Depends(principal_id),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> JSONResponse:
        session, etag = service.decide_result(
            session_id,
            owner_principal_id=owner,
            decision=request.decision,
            feedback=request.feedback,
            expected_etag=normalize_session_etag(if_match),
        )
        return _session_response(session, etag)

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
    document, falling back to the principal id on any malformation."""

    if encoded_principal is None:
        return fallback
    try:
        payload = json.loads(
            base64.b64decode(encoded_principal, validate=True).decode("utf-8")
        )
    except (
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        UnicodeEncodeError,
    ):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    name_type = payload.get("name_typ")
    claims = payload.get("claims")
    if not isinstance(name_type, str) or not isinstance(claims, list):
        return fallback
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("typ") != name_type:
            continue
        value = claim.get("val")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _session_response(
    session: QuestionSession,
    etag: str,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    return JSONResponse(
        content=session.model_dump(mode="json"),
        status_code=status_code,
        headers={"ETag": f'"{etag}"'},
    )


def _execution_job_response(job: ExecutionJob) -> JSONResponse:
    return JSONResponse(content=job.model_dump(mode="json"))


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    return any(
        candidate == "*" or candidate.removeprefix("W/") == etag
        for candidate in (
            item.strip() for item in if_none_match.split(",")
        )
    )
