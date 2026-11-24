"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api, health
from app.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, correlation_id_var
from app.orchestrator import Orchestrator
from app.services import Services

log = logging.getLogger("app")


def _configure_telemetry(settings: Settings) -> None:
    """Azure Monitor OpenTelemetry distro: traces, metrics and logs -> App Insights."""
    if not settings.applicationinsights_connection_string:
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore[import-not-found]

        configure_azure_monitor(connection_string=settings.applicationinsights_connection_string)
        log.info("telemetry_enabled")
    except ImportError:
        log.warning("telemetry_package_missing")


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    _configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or Services.build(settings)
        app.state.services = svc
        app.state.orchestrator = Orchestrator(svc)
        log.info(
            "startup",
            extra={
                "env": settings.environment,
                "version": settings.app_version,
                "auth_mode": settings.auth_mode,
                "llm": svc.llm.name,
                "kb_chunks": len(svc.knowledge.chunks),
                "kb_version": svc.knowledge.version,
            },
        )
        yield
        await svc.aclose()

    app = FastAPI(
        title="PharmaFieldAssist API",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "prod" else None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "X-PFA-Token", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        incoming = request.headers.get("x-correlation-id", "")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.replace("-", "").isalnum() else str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/health"):
            log.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correlation_id": cid,
                },
            )
        return response

    def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "details": details or {}, "correlation_id": correlation_id_var.get()}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", extra={"code": exc.code, "error": exc.message})
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()[:5]]
        return _error(422, "invalid_request", "Request validation failed", {"errors": errors})

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return _error(500, "internal_error", "An unexpected error occurred")

    app.include_router(health)
    app.include_router(api)
    return app


app = create_app()
