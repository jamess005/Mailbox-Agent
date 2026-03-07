"""
db.py — MySQL connection, matching the pattern in notebooks/01_data_checks.ipynb
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
load_dotenv()

_engine: Engine | None = None
_readonly_engine: Engine | None = None


def _build_engine(user: str, password: str) -> Engine:
    url = (
        f"mysql+mysqlconnector://"
        f"{user}:{password}"
        f"@{os.environ['MYSQL_HOST']}/{os.environ['MYSQL_DB']}"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
    )


def get_engine() -> Engine:
    """Read-write engine — used for invoice storage and validation lookups."""
    global _engine
    if _engine is None:
        _engine = _build_engine(
            os.environ['MYSQL_USER'],
            os.environ['MYSQL_PASSWORD'],
        )
    return _engine


def get_readonly_engine() -> Engine:
    """Read-only engine — used exclusively by the query agent.
    Falls back to the main engine if MYSQL_READONLY_USER is not configured."""
    global _readonly_engine
    if _readonly_engine is None:
        ro_user = os.environ.get('MYSQL_READONLY_USER')
        ro_pass = os.environ.get('MYSQL_READONLY_PASSWORD')
        if ro_user and ro_pass:
            _readonly_engine = _build_engine(ro_user, ro_pass)
        else:
            # Fallback: main engine (e.g. during local dev without the RO user set up)
            _readonly_engine = get_engine()
    return _readonly_engine


def query(sql: str, params: dict | None = None) -> list[dict]:
    """Run a SELECT via the read-write engine (validation lookups, etc.)."""
    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        keys = result.keys()
        return [dict(zip(keys, row)) for row in result.fetchall()]


def query_readonly(sql: str, params: dict | None = None) -> list[dict]:
    """Run a SELECT via the read-only engine — for the query agent pipeline."""
    with get_readonly_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        keys = result.keys()
        return [dict(zip(keys, row)) for row in result.fetchall()]


def execute(sql: str, params: dict | None = None):
    """Run INSERT / UPDATE inside a transaction."""
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})