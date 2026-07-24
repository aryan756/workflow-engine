"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.deps import build_engine
from app.api.routes import router
from app.config import get_settings
from app.db import dispose_db, get_session_factory, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    app.state.settings = settings
    app.state.engine = build_engine(settings, get_session_factory())
    logger.info(
        "Agentic Workflow Engine %s ready (provider=%s)",
        __version__,
        app.state.engine.provider.name,
    )
    try:
        yield
    finally:
        await dispose_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agentic Workflow Engine",
        version=__version__,
        description=(
            "DAG workflow runtime with agent/decision nodes, conditional "
            "branching, validated agent output, idempotent tool calls, "
            "human approval gates, retry/resume and full per-node tracing."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "agentic-workflow-engine",
            "version": __version__,
            "docs": "/docs",
            "api": "/api",
        }

    return app


app = create_app()
