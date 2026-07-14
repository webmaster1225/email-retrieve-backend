from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import ai, auth, contacts, export, messages, outreach, sync

settings = get_settings()
app = FastAPI(title="Relationship Intelligence CRM", version="0.1.0")


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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


app.include_router(auth.router, prefix="/api/v1")
app.include_router(messages.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(sync.router, prefix="/api/v1")
app.include_router(contacts.router, prefix="/api/v1")
app.include_router(outreach.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
