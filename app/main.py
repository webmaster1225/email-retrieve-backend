from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.routers import ai, auth, contacts, export, messages, outreach, sync, accounts, campaigns

settings = get_settings()
app = FastAPI(title="Relationship Intelligence CRM", version="0.1.0")
logger = logging.getLogger(__name__)
_schedule_task: asyncio.Task | None = None


def _apply_cors_headers(request: Request, response: JSONResponse) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") in settings.cors_origin_list:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UnhandledErrorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            return _apply_cors_headers(
                request,
                JSONResponse(status_code=500, content={"detail": str(exc)}),
            )


app.add_middleware(UnhandledErrorMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _apply_cors_headers(
        request,
        JSONResponse(status_code=500, content={"detail": str(exc)}),
    )


async def _schedule_sweep_loop() -> None:
    from app.services.campaign_send import process_due_scheduled

    while True:
        try:
            interval = max(15.0, float(get_settings().schedule_sweep_seconds))
            db = SessionLocal()
            try:
                n = await process_due_scheduled(db)
                if n:
                    logger.info("Processed %s due scheduled send(s)", n)
            finally:
                db.close()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Schedule sweep failed")
            await asyncio.sleep(30)


@app.on_event("startup")
async def on_startup() -> None:
    global _schedule_task
    init_db()
    # Immediate sweep once at boot
    try:
        from app.services.campaign_send import process_due_scheduled

        db = SessionLocal()
        try:
            await process_due_scheduled(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Startup schedule sweep failed")
    _schedule_task = asyncio.create_task(_schedule_sweep_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _schedule_task
    if _schedule_task:
        _schedule_task.cancel()
        try:
            await _schedule_task
        except asyncio.CancelledError:
            pass
        _schedule_task = None


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "database": settings.database_url.split("://", 1)[0]}


app.include_router(auth.router, prefix="/api/v1")
app.include_router(accounts.router, prefix="/api/v1")
app.include_router(campaigns.router, prefix="/api/v1")
app.include_router(messages.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(sync.router, prefix="/api/v1")
app.include_router(contacts.router, prefix="/api/v1")
app.include_router(outreach.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
