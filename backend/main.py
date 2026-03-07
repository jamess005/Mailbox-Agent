"""
main.py — FastAPI entry point

Start with:
    cd backend
    uvicorn main:app --reload --port 8000
"""

import os
import sys

# Ensure backend/ is on the path so all sub-packages resolve correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# .env lives one level up from backend/ (at project root)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
load_dotenv(dotenv_path=_env_path)
load_dotenv()  # fallback: cwd

# Deferred import — after sys.path and .env are set
from agents.orchestrator import Orchestrator

_orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator
    print("[startup] Initialising orchestrator…")
    print(f"[startup] .env path: {os.path.abspath(_env_path)}")
    print(f"[startup] DB host:   {os.environ.get('MYSQL_HOST', 'NOT SET')}")
    print(f"[startup] LLM path:  {os.environ.get('LLM_MODEL_PATH', 'NOT SET')}")
    _orchestrator = Orchestrator()
    print("[startup] Ready.")
    yield


app = FastAPI(title="Mailbox-Agent", lifespan=lifespan)

# Allowed origins — restrict to local dev + common local hosts.
# In production, replace with the actual frontend domain.
_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5500",
    "http://localhost:5501",
    "http://localhost:8000",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:5501",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── Input validation constants ──
_MAX_BASE64_BYTES   = 25 * 1024 * 1024   # 25 MB decoded
_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/tiff", "application/pdf"}
_VALID_PIPELINES    = {
    "invoice_extraction", "supplier_query",
    "combined_verification", "invoice_approval",
    "invoice_batch", "batch_approval",
}
_MAX_BATCH_SIZE = 10

# ── Simple in-memory rate limiter ──
import time as _time
from collections import defaultdict as _defaultdict

_rate_limits: dict[str, list[float]] = _defaultdict(list)
_RATE_WINDOW  = 60   # seconds
_RATE_MAX     = 30   # max requests per window per IP


def _check_rate(ip: str) -> bool:
    now = _time.time()
    bucket = _rate_limits[ip]
    # Purge old entries
    _rate_limits[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(_rate_limits[ip]) >= _RATE_MAX:
        return False
    _rate_limits[ip].append(now)
    return True


class EmailPayload(BaseModel):
    pipeline:       str
    timestamp:      str
    email:          dict
    invoice:        dict | None = None
    invoices:       list[dict] | None = None
    invoice_number: str | None = None
    decision:       str | None = None
    batch_key:      str | None = None


@app.post("/api/process")
async def process(payload: EmailPayload, request: Request):
    # ── Rate limiting ──
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please wait before retrying."},
        )

    if _orchestrator is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "Server is initialising — please retry in a few seconds."},
            headers={"Retry-After": "5"},
        )

    # ── Validate pipeline name ──
    if payload.pipeline not in _VALID_PIPELINES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid pipeline: {payload.pipeline}"},
        )

    # ── Validate attachment if present ──
    if payload.invoice:
        mime = payload.invoice.get("mime_type", "")
        if mime and mime not in _ALLOWED_MIME_TYPES:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Unsupported file type: {mime}"},
            )
        b64 = payload.invoice.get("base64_data", "")
        if b64 and len(b64) * 3 // 4 > _MAX_BASE64_BYTES:
            return JSONResponse(
                status_code=400,
                content={"detail": "Attachment too large (max 25 MB)."},
            )

    # ── Approval replies bypass LangGraph ──
    if payload.pipeline == "invoice_approval":
        from agents.invoice_agent import run_approval
        return run_approval(
            invoice_number=(payload.invoice_number or "").strip(),
            decision=(payload.decision or "").lower().strip(),
            sender=payload.email.get("from", "unknown"),
        )

    # ── Batch approval bypass ──
    if payload.pipeline == "batch_approval":
        from agents.invoice_agent import run_batch_approval
        return run_batch_approval(
            batch_key=(payload.batch_key or "").strip(),
            decision=(payload.decision or "").lower().strip(),
            sender=payload.email.get("from", "unknown"),
        )

    # ── Batch invoice processing bypass ──
    if payload.pipeline == "invoice_batch":
        if not payload.invoices:
            return JSONResponse(
                status_code=400,
                content={"detail": "No invoices provided in batch."},
            )
        if len(payload.invoices) > _MAX_BATCH_SIZE:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Maximum {_MAX_BATCH_SIZE} invoices per batch."},
            )
        for i, inv in enumerate(payload.invoices, 1):
            mime = inv.get("mime_type", "")
            if mime and mime not in _ALLOWED_MIME_TYPES:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Invoice {i}: unsupported file type {mime}"},
                )
            b64 = inv.get("base64_data", "")
            if b64 and len(b64) * 3 // 4 > _MAX_BASE64_BYTES:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Invoice {i}: file too large (max 25 MB)."},
                )
        from agents.invoice_agent import run_batch
        return run_batch(
            invoices=payload.invoices,
            email=payload.email,
            route="invoice_extraction",
            llm=_orchestrator.llm,
        )

    try:
        return await _orchestrator.run(payload.model_dump())
    except Exception as exc:
        # Log full trace server-side, return safe message to client
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred. Please try again."},
        )


@app.get("/health")
def health():
    return {
        "status":       "ok",
        "model_loaded": _orchestrator is not None,
        "llm_path":     os.environ.get("LLM_MODEL_PATH", "not set"),
        "db_host":      os.environ.get("MYSQL_HOST", "not set"),
    }