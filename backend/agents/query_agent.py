"""
query_agent.py — Natural language → SQL → DB → reply
"""

from __future__ import annotations
import os, sys, re

_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from agents.db import query as db_query

# ── Exact schema matching the real MySQL tables ──
DB_SCHEMA = """\
Tables:
  suppliers   (id INT PK, name, address, tax_id, iban, email, created_at)
  clients     (id INT PK, name, address, tax_id, email, created_at)
  invoices    (id INT AUTO_INCREMENT PK -- internal only, never shown to users,
               invoice_number VARCHAR(50) -- the human-visible invoice reference,
               date_of_issue,
               supplier_id  → suppliers.id,
               client_id    → clients.id,
               total_net_worth, vat_percent, total_vat_amount,
               total_gross_worth, raw_data JSON, created_at)
  line_items  (id INT PK, invoice_id → invoices.id, item_number,
               description, quantity, unit_of_measure,
               net_price, net_worth, vat_percent, gross_worth)

Key relationships:
  invoices.supplier_id = suppliers.id
  invoices.client_id   = clients.id
  line_items.invoice_id = invoices.id

CRITICAL:
  invoices.id is an internal auto-increment key -- NEVER use it in WHERE clauses.
  invoices.invoice_number is the user-facing reference (VARCHAR) -- ALWAYS filter
  by invoice_number when looking up a specific invoice.
  Example: WHERE i.invoice_number = '<number>' (use quotes, it is VARCHAR).
  NOTE: Using invoices.id in JOIN conditions IS correct and required
  (e.g. JOIN line_items l ON l.invoice_id = i.id). Only avoid i.id in WHERE."""

SQL_PROMPT = PromptTemplate.from_template("""\
You are a read-only SQL assistant for a MySQL invoice database.
Generate a single valid SELECT statement that answers the question.
Rules:
- Use ONLY SELECT. No INSERT, UPDATE, DELETE, DROP.
- Use exact table and column names from the schema below.
- Always use table aliases and qualify column names (e.g. i.invoice_number, s.name).
- invoice_number is VARCHAR -- filter with: WHERE i.invoice_number = '...' (use quotes).
- NEVER reference invoices.id in WHERE clauses -- it is internal. Do not write subqueries
  like (SELECT id FROM invoices ...). Use invoice_number directly instead.
- Keep the WHERE clause minimal -- one condition per concept, no redundant repeats.
- For exact matches use =. Only use LIKE if the user asks for a partial/fuzzy search.
- Do NOT use subqueries unless the question explicitly requires aggregation across tables.
- When querying invoices, always JOIN suppliers s ON s.id = i.supplier_id and
  JOIN clients c ON c.id = i.client_id and include s.name AS supplier_name and
  c.name AS client_name in the SELECT list, so results can be distinguished.
- Return ONLY the SQL statement. No explanation, no markdown, no backticks.

Schema:
{schema}

Question: {question}

SQL:""")

ANSWER_PROMPT = PromptTemplate.from_template("""\
The user asked: {question}
Database results: {results}
Write a concise professional answer listing only the key facts.
Format each datum on its own line with a label, e.g.:
  Invoice Number: 12345
  Supplier: Acme Corp
  Total: $1,234.56
Do not write code, do not add disclaimers, do not explain the query. Plain text only:""")


def run(email: dict, llm) -> dict:
    sender   = email.get("from", "unknown")
    question = (email.get("body") or email.get("subject", "")).strip()

    if not question:
        return _err(sender, "Your message had no question body. Please describe what you need.")

    # Override max_new_tokens for SQL generation (orchestrator default is 32)
    _set_max_tokens(llm, 512)

    sql = _generate_sql(question, llm)
    if not sql:
        _set_max_tokens(llm, 32)
        return _err(sender, f"Could not generate a database query for: \"{question}\"")

    print(f"[query_agent] Generated SQL:\n  {sql}")

    rows, err = _safe_query(sql)
    if err:
        print(f"[query_agent] SQL error: {err}")
        _set_max_tokens(llm, 512)
        sql = _retry_sql(question, sql, err, llm)
        if sql:
            print(f"[query_agent] Retry SQL:\n  {sql}")
            rows, err = _safe_query(sql)
        if err:
            _set_max_tokens(llm, 32)
            return _err(sender, f"Database query failed: {err}")

    _set_max_tokens(llm, 150)  # keep higher for answer generation
    answer = _format_answer(question, rows, llm)
    _set_max_tokens(llm, 32)   # restore

    # Build a professional email body with an intro line
    intro = _build_intro(question, rows)
    body  = f"Dear {sender},\n\n{intro}\n\n{answer}\n\nRegards,\nAIMailbox"

    return {
        "status":     "ok",
        "answer":     answer,
        "sql":        sql,
        "email_body": body,
    }


def _set_max_tokens(llm, n: int):
    """Override max_new_tokens on the underlying HF pipeline."""
    try:
        llm.pipeline._forward_params["max_new_tokens"] = n
    except Exception:
        pass


def _generate_sql(question: str, llm) -> str:
    chain  = SQL_PROMPT | llm | StrOutputParser()
    raw    = chain.invoke({"schema": DB_SCHEMA, "question": question})
    sql    = _extract_sql(raw)
    if sql:
        sql = _sanitize_sql(sql)
    return sql


RETRY_PROMPT = PromptTemplate.from_template("""\
The following SQL failed with an error. Fix it.
- Use exact table and column names from the schema.
- The JOIN condition line_items.invoice_id = invoices.id is CORRECT — do NOT change it.
  Only avoid invoices.id in WHERE filter conditions, not in JOINs.
- Return ONLY the corrected SQL. No explanation, no markdown, no backticks.

Schema:
{schema}

Original SQL: {sql}
Error: {error}

Corrected SQL:""")


def _retry_sql(question: str, bad_sql: str, error: str, llm) -> str:
    chain = RETRY_PROMPT | llm | StrOutputParser()
    raw   = chain.invoke({"schema": DB_SCHEMA, "sql": bad_sql, "error": error})
    sql   = _extract_sql(raw)
    if sql:
        sql = _sanitize_sql(sql)
    return sql


def _extract_sql(raw: str) -> str:
    sql = raw.strip()
    # Strip markdown fences wrapping the whole output
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # Truncate at any remaining ``` (LLM repeating SQL in a fence block)
    if "```" in sql:
        sql = sql[:sql.index("```")].strip()
    # Truncate at LLM explanation noise that starts on a new line
    for marker in ("\nExplanation", "\nNote:", "\nNote ", "\nThis SQL",
                   "\nThe final answer", "\nHere is", "\nThis query",
                   "\nI ", "\nHowever", "\nPlease", "\nThe above",
                   "\n--"):
        idx = sql.find(marker)
        if idx > 0:
            sql = sql[:idx].strip()
    # Strip inline SQL comments (-- ...)
    sql = re.sub(r'--[^\n]*', '', sql).strip()
    # Take only the first statement
    sql = sql.split(";")[0].strip()
    if not sql.upper().startswith("SELECT"):
        print(f"[query_agent] Non-SELECT discarded: {sql}")
        return ""
    # Detect obvious truncation: ends mid-token (no closing paren balance or dangling AND/OR/WHERE)
    upper = sql.upper().rstrip()
    if re.search(r'\b(AND|OR|WHERE|JOIN|ON|FROM|SELECT|,)\s*$', upper):
        print(f"[query_agent] SQL appears truncated, discarding.")
        return ""
    return sql


# Pattern: AND i.id = (SELECT ...) or AND i.id IN (SELECT ...) from any table
# Also matches truncated subqueries that have no closing paren (end of string).
_ID_SUBQUERY_RE = re.compile(
    r'\s*AND\s+\w+\.id\s*(?:=|IN)\s*\(\s*SELECT\s[^)]*(?:\)|$)',
    re.IGNORECASE,
)


def _sanitize_sql(sql: str) -> str:
    """Strip LLM-generated subquery noise and collapse duplicate WHERE conditions."""
    # Remove AND *.id = (SELECT ...) or AND *.id IN (SELECT ...) clauses
    cleaned = _ID_SUBQUERY_RE.sub('', sql)

    # Dedup conditions within the WHERE clause only
    m = re.split(r'\bWHERE\b', cleaned, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        prefix, where = m[0], m[1]
        parts = re.split(r'\bAND\b', where, flags=re.IGNORECASE)
        seen, deduped = set(), []
        for part in parts:
            norm = re.sub(r'\s+', ' ', part.strip().lower())
            if norm and norm not in seen:
                seen.add(norm)
                deduped.append(part)
        result = prefix + 'WHERE' + ' AND '.join(deduped)
    else:
        result = cleaned

    result = result.strip()
    if result != sql:
        print(f"[query_agent] SQL sanitized:\n  {result}")
    return result


def _safe_query(sql: str) -> tuple[list[dict], str | None]:
    if not sql.strip().upper().startswith("SELECT"):
        return [], "Only SELECT queries permitted."
    # Defence: reject dangerous keywords
    upper = sql.upper()
    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"):
        if re.search(rf'\b{kw}\b', upper):
            return [], f"Blocked: {kw} not permitted."
    try:
        return db_query(sql), None
    except Exception as e:
        return [], str(e)


def _format_answer(question: str, rows: list[dict], llm) -> str:
    if not rows:
        return "No matching records were found in the database for your query."

    # Always use the structured formatter for invoice/line-item results —
    # avoids LLM blob formatting and gives consistent spacing.
    if _has_invoice_keys(rows[0]):
        return _structured_rows(rows)

    results_str = "\n".join(str(r) for r in rows[:20])
    _set_max_tokens(llm, 150)
    chain  = ANSWER_PROMPT | llm | StrOutputParser()
    raw = (chain.invoke({"question": question, "results": results_str})).strip()
    for marker in ("```", "## Step", "Note:", "Disclaimer", "---"):
        if marker in raw:
            raw = raw[:raw.index(marker)].strip()
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    return paragraphs[0] if paragraphs else raw


def _err(sender: str, msg: str) -> dict:
    return {"status": "error", "email_body": f"Dear {sender},\n\n{msg}\n\nRegards,\nAIMailbox"}


def _build_intro(question: str, rows: list[dict]) -> str:
    """Build a one-line intro sentence that contextualises the data that follows."""
    count = len(rows)
    # Try to identify an invoice number from the question or the result
    inv_num = None
    m = re.search(r'\b(\d{2,})\b', question)
    if m:
        inv_num = m.group(1)
    elif rows and "invoice_number" in rows[0]:
        inv_num = str(rows[0]["invoice_number"])

    # Detect what kind of data was returned
    keys = set(rows[0].keys()) if rows else set()
    if keys & {"item_number", "description", "net_price", "net_worth"}:
        noun = "line item" if count == 1 else "line items"
    elif keys & {"total_net_worth", "total_gross_worth", "total_vat_amount"}:
        noun = "totals summary" if count == 1 else "invoice records"
    elif keys & {"name", "address", "tax_id", "iban"}:
        noun = "supplier record" if count == 1 else "supplier records"
    else:
        noun = "record" if count == 1 else "records"

    if inv_num:
        if "total" in noun:
            return f"Here is the {noun} for invoice {inv_num}:"
        if count == 1:
            return f"Here is the {noun} for invoice {inv_num}:"
        return f"Here are the {count} {noun} for invoice {inv_num}:"
    if count == 1:
        return f"Here is the {noun} matching your query:"
    return f"Here are the {count} {noun} matching your query:"


# ── Structured answer helpers ──

# Column names that signal an invoice-like result
_INVOICE_KEYS = {"invoice_number", "total_net_worth", "total_gross_worth", "name",
                 "supplier_id", "date_of_issue", "description"}

_LABEL_MAP = {
    "invoice_number":    "Invoice Number",
    "date_of_issue":     "Date of Issue",
    "name":              "Supplier",
    "total_net_worth":   "Net Worth",
    "total_vat_amount":  "VAT Amount",
    "vat_percent":       "VAT %",
    "total_gross_worth": "Gross Total",
    "address":           "Address",
    "tax_id":            "Tax ID",
    "iban":              "IBAN",
    "description":       "Description",
    "quantity":          "Quantity",
    "unit_of_measure":   "Unit",
    "net_price":         "Unit Price",
    "net_worth":         "Line Net",
    "gross_worth":       "Line Gross",
}


_SKIP_KEYS = {"id", "supplier_id", "client_id", "invoice_id", "raw_data", "created_at"}


def _has_invoice_keys(row: dict) -> bool:
    return bool(set(row.keys()) & _INVOICE_KEYS)


def _structured_rows(rows: list[dict]) -> str:
    """Format DB rows into a clean, human-readable block with a blank line between entries.

    Fields whose value is identical across ALL rows (e.g. invoice_number repeated for
    every line item) are hoisted to a shared header shown once at the top.
    None / empty values are always hidden.
    """
    data_keys = [k for k in rows[0].keys() if k not in _SKIP_KEYS]

    # Hoist fields that are constant AND non-null across all rows
    header_keys, item_keys = [], []
    if len(rows) > 1:
        for key in data_keys:
            vals = {str(r.get(key)) for r in rows}
            if len(vals) == 1 and _is_present(rows[0].get(key)):
                header_keys.append(key)
            else:
                item_keys.append(key)
    else:
        item_keys = data_keys

    parts = []

    if header_keys:
        hdr = []
        for key in header_keys:
            label = _LABEL_MAP.get(key, key.replace("_", " ").title())
            hdr.append(f"  {label}: {rows[0].get(key)}")
        parts.append("\n".join(hdr))

    for i, row in enumerate(rows, 1):
        lines = []
        for key in item_keys:
            val = row.get(key)
            if not _is_present(val):
                continue
            label = _LABEL_MAP.get(key, key.replace("_", " ").title())
            lines.append(f"  {label}: {val}")
        if lines:
            parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _is_present(val) -> bool:
    """Return True if the value is meaningful (not None, not empty, not 'None')."""
    if val is None:
        return False
    s = str(val).strip()
    return s != "" and s.lower() != "none"