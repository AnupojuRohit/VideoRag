import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.errors import APIError
from app.core.logging_config import configure_logging
from app.db.session import engine
from app.api.routes.videos import router as videos_router

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(videos_router, prefix="/api/v1")


@app.exception_handler(APIError)
async def handle_api_error(_, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    _,
    __: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "invalid_request",
                "message": "The request was invalid. Check the supplied fields and try again.",
            }
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def check_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis() -> None:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        await client.ping()
    finally:
        await client.aclose()


@app.get("/ready")
async def readiness(response: Response) -> dict[str, object]:
    checks = {"postgres": True, "redis": True}
    try:
        await check_database()
    except (SQLAlchemyError, OSError) as exc:
        checks["postgres"] = False
        logger.warning(
            "Readiness dependency unavailable",
            extra={"dependency": "postgres", "error_type": type(exc).__name__},
        )
    try:
        await check_redis()
    except (RedisError, OSError) as exc:
        checks["redis"] = False
        logger.warning(
            "Readiness dependency unavailable",
            extra={"dependency": "redis", "error_type": type(exc).__name__},
        )

    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}
