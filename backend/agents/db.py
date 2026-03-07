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


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = (
            f"mysql+mysqlconnector://"
            f"{os.environ['MYSQL_USER']}:{os.environ['MYSQL_PASSWORD']}"
            f"@{os.environ['MYSQL_HOST']}/{os.environ['MYSQL_DB']}"
        )
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def query(sql: str, params: dict | None = None) -> list[dict]:
    """Run a SELECT, return list of dicts."""
    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        keys = result.keys()
        return [dict(zip(keys, row)) for row in result.fetchall()]


def execute(sql: str, params: dict | None = None):
    """Run INSERT / UPDATE inside a transaction."""
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})