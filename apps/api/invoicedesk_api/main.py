"""FastAPI gateway (DESIGN §4.1 "API gateway"; docs/api-contract.md is authoritative).

``uvicorn invoicedesk_api.main:app``. The app builds its :class:`~invoicedesk_api.runtime.Runtime`
on startup (offline fakes by default) unless one is injected with :func:`create_app` (tests).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from invoicedesk_api.errors import install_error_handlers
from invoicedesk_api.routes import actions, agent, documents, events, ingest, owner, tryit
from invoicedesk_api.runtime import Runtime, build_runtime
from invoicedesk_api.settings import get_api_settings

__all__ = ["API_PREFIX", "app", "create_app"]

API_PREFIX = "/api/v1"
log = logging.getLogger("invoicedesk.api")


def create_app(runtime: Runtime | None = None, *, start_jobs: bool | None = None) -> FastAPI:
    """``runtime`` injected: used as is (the caller owns its lifecycle). Otherwise one is built
    from the environment at startup and closed at shutdown. ``start_jobs`` overrides
    ``API_EMBEDDED_WORKER`` for the embedded pipeline job threads."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        rt = runtime or build_runtime()
        app.state.runtime = rt
        run_jobs = rt.api.embedded_worker if start_jobs is None else start_jobs
        if run_jobs:
            rt.start()
        try:
            yield
        finally:
            if owned:
                rt.close()
            elif run_jobs and rt.runner is not None:
                rt.runner.stop()
                rt.runner = None

    app = FastAPI(
        title="InvoiceDesk API",
        version="0.1.0",
        description="AI accounts-payable clerk: works with QuickBooks Online (sandbox).",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    if runtime is not None:
        app.state.runtime = runtime
    origins = [
        o.strip()
        for o in (runtime.api if runtime else get_api_settings()).web_origins.split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Dev-User"],
    )
    install_error_handlers(app)
    for r in (
        ingest.router,
        documents.router,
        actions.router,
        owner.router,
        tryit.router,
        agent.router,
        events.router,
    ):
        app.include_router(r, prefix=API_PREFIX)
    # Signed LocalStorage URLs: the contract lists /files under /api/v1; LocalStorage builds
    # them as {API_BASE_URL}/files/..., so both paths are served.
    app.include_router(documents.files_router, prefix=API_PREFIX)
    app.include_router(documents.files_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
