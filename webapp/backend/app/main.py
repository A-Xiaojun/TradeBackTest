from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.equity import router as equity_router
from app.api.v1.health import router as health_router
from app.api.v1.ingest import router as ingest_router
from app.api.v1.strategies import router as strategies_router
from app.api.v1.trades import router as trades_router
from app.core.db import init_db


def create_app() -> FastAPI:
    app = FastAPI(title="TradeBackTest Web API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(strategies_router, prefix="/api/v1")
    app.include_router(equity_router, prefix="/api/v1")
    app.include_router(trades_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")

    @app.on_event("startup")
    def on_startup() -> None:
        init_db()

    return app


app = create_app()
