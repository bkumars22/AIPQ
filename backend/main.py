"""
AIPQ FastAPI backend — app setup, CORS, auth, rate limiting, lifespan.

Authentication is applied per-route via Depends(get_auth_context) (see
auth/dependencies.py) rather than as blanket middleware, so public routes
(health check) can opt out cleanly — this is the standard FastAPI pattern
and avoids reimplementing route matching that Depends already does.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from db.session import create_pg_pool, create_redis_client
from rate_limit import limiter
from routers import ab_tests, drift, golden_cases, metrics, projects, prompts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aipq.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Connecting PostgreSQL pool...")
    app.state.pg_pool = await create_pg_pool()
    logger.info("Connecting Redis...")
    app.state.redis = create_redis_client()

    yield

    logger.info("Closing Redis connection...")
    await app.state.redis.aclose()
    logger.info("Closing PostgreSQL pool...")
    await app.state.pg_pool.close()


app = FastAPI(title="AIPQ Backend", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(prompts.router)
app.include_router(drift.router)
app.include_router(golden_cases.router)
app.include_router(metrics.router)
app.include_router(ab_tests.router)


@app.get("/health")
@limiter.exempt
async def health_check():
    return {"status": "ok"}
