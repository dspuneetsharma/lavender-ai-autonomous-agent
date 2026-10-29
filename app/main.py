"""HTTP API: POST /agent runs the agent, GET /documents/{name} serves a finished .docx, GET /health."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent.pipeline import AgentPipeline
from app.config import PROJECT_ROOT, Settings, configure_logging, get_settings
from app.docx_builder import DocxRenderer, is_safe_document_name
from app.llm import build_llm_client
from app.schemas import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
STATIC_DIR = PROJECT_ROOT / "static"

_ERROR_RESPONSES = {
    400: {"model": AgentResponse, "description": "Refused by an input screen (no LLM call made)"},
    422: {"description": "Invalid body, or the request is not asking for a document"},
    429: {"model": AgentResponse, "description": "The model provider's free daily usage limit is used up"},
    502: {"model": AgentResponse, "description": "The agent could not produce a valid document"},
}


def create_app(settings: Settings | None = None, pipeline: AgentPipeline | None = None) -> FastAPI:
    """Build the app. Without a pipeline, the real Groq-backed one is created at startup."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        if pipeline is None:
            client = build_llm_client(settings)
            app.state.pipeline = AgentPipeline(client, DocxRenderer(settings.output_dir), settings)
        logger.info("Agent ready (model %s)", settings.groq_model)
        yield

    app = FastAPI(
        title="Lavender AI Autonomous Agent",
        description="Plans, writes, checks and renders Word documents from a plain-language request.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline
    app.state.settings = settings

    @app.post("/agent", response_model=AgentResponse, responses=_ERROR_RESPONSES)
    def run_agent(body: AgentRequest, request: Request) -> JSONResponse:
        """Run the agent on one request and return its plan, status, warnings and document link."""
        result = request.app.state.pipeline.run(body.request)
        response = result.response
        if result.document_name is not None:
            url = request.url_for("download_document", name=result.document_name)
            response = response.model_copy(update={"document_url": str(url)})
        return JSONResponse(status_code=result.http_status, content=response.model_dump())

    @app.get("/documents/{name}", response_class=FileResponse, name="download_document")
    def download_document(name: str, request: Request) -> FileResponse:
        """Download a generated document. Only names created by the agent are served."""
        output_dir = request.app.state.settings.output_dir.resolve()
        path = (output_dir / name).resolve()
        if not is_safe_document_name(name) or path.parent != output_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="Document not found")
        download_name = name.split("-", 1)[1]
        return FileResponse(path, media_type=DOCX_MEDIA_TYPE, filename=download_name)

    @app.get("/health")
    def health(request: Request) -> dict[str, str | int]:
        """Liveness check plus the limits a client needs; makes no LLM call."""
        current = request.app.state.settings
        return {"status": "ok", "model": current.groq_model, "max_request_chars": current.max_request_chars}

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unexpected error on %s", request.url.path)
        body = AgentResponse(status="failed", message="Internal error; see the server log.")
        return JSONResponse(status_code=500, content=body.model_dump())

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")  # after API routes
    return app


app = create_app()
