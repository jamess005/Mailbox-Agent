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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# .env lives two levels up from backend/ (at ml-proj/ level)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env')
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class EmailPayload(BaseModel):
    pipeline:       str
    timestamp:      str
    email:          dict
    invoice:        dict | None = None
    invoice_number: str | None = None
    decision:       str | None = None


@app.post("/api/process")
async def process(payload: EmailPayload):
    if _orchestrator is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "Server is initialising — please retry in a few seconds."},
            headers={"Retry-After": "5"},
        )

    # Approval replies bypass LangGraph — handled directly in invoice_agent
    if payload.pipeline == "invoice_approval":
        from agents.invoice_agent import run_approval
        return run_approval(
            invoice_number=(payload.invoice_number or "").strip(),
            decision=(payload.decision or "").lower().strip(),
            sender=payload.email.get("from", "unknown"),
        )

    return await _orchestrator.run(payload.model_dump())


@app.get("/health")
def health():
    return {
        "status":       "ok",
        "model_loaded": _orchestrator is not None,
        "llm_path":     os.environ.get("LLM_MODEL_PATH", "not set"),
        "db_host":      os.environ.get("MYSQL_HOST", "not set"),
    }