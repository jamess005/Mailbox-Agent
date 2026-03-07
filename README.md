# AIMailbox

An autonomous invoice processing and supplier query system built around a simulated email client. Emails are classified and routed through one of several pipelines — invoice extraction, supplier database query, combined verification, or batch processing — and professional reply emails are drafted and returned automatically.

[![LinkedIn](https://img.shields.io/badge/LinkedIn-James%20Scott-0077B5?logo=linkedin)](https://www.linkedin.com/in/jamesscott005)

---

## Overview

The system simulates a dedicated mailbox (`AIMailbox@gmail.com`) that receives and processes:

- **Invoice attachments** (single or batch up to 10) — OCR'd via DocLing, fields extracted by Llama 3.1 8B, validated against the database for mathematical consistency, supplier registration, and duplicate detection. Passes through a human-in-the-loop approval flow before storage.
- **Supplier / database queries** — natural language questions parsed by the LLM into SQL, executed against a MySQL invoice database, with results returned as formatted reply emails.
- **Combined emails** — attachment extraction and query resolution run sequentially within the same thread.

All results are rendered as email threads in a dark-themed frontend client.

---

## Architecture

```
Frontend (Vanilla HTML/CSS/JS)
  │
  └─ POST /api/process ──── FastAPI (main.py)
        │                     ├─ Rate limiting (30 req/min/IP)
        │                     ├─ CORS (localhost only)
        │                     ├─ Input validation (MIME, size, pipeline)
        │                     └─ Pipeline routing
        │
        ├─ orchestrator.py ─── LangGraph state machine
        │     │                  classify → invoice_node | query_node → format_reply
        │     │
        │     ├─ invoice_agent.py ─── DocLing OCR → LLM JSON extraction → validation
        │     │     │                   ├─ Single invoice processing
        │     │     │                   ├─ Batch processing (up to 10)
        │     │     │                   └─ Accept / Decline approval flow
        │     │     │
        │     │     └─ validation.py
        │     │           ├─ Required fields check
        │     │           ├─ Arithmetic verification (line items, VAT, gross)
        │     │           ├─ Supplier registration lookup
        │     │           └─ Duplicate invoice detection
        │     │
        │     └─ query_agent.py ─── NL → SQL generation → execution → answer
        │           ├─ SQL blocklist (UNION, DROP, LOAD_FILE, stacked queries)
        │           ├─ Auto LIMIT 50
        │           └─ Retry on SQL error
        │
        └─ email_draft.py ─── Reply email templates
              ├─ Approval request (single / batch)
              ├─ Confirmed / Declined
              └─ Rejection with per-invoice status
```

---

## Pipelines

### `invoice_extraction`
Triggered when an email contains a single invoice attachment (PNG, JPG, PDF, TIFF).

1. **OCR** — DocLing converts the document to markdown using EasyOCR (CPU-only, English)
2. **LLM extraction** — Llama 3.1 8B parses the markdown into structured JSON (invoice number, dates, supplier, client, line items, totals, VAT, shipping)
3. **Validation** — four checks run against the extracted data:
   - **Required fields** — invoice number, date, supplier name, gross total
   - **Arithmetic** — line item qty × price = net_worth, line totals sum to net, net + VAT + shipping = gross
   - **Supplier** — is the supplier registered in the `suppliers` table?
   - **Duplicate** — has this invoice number been submitted before?
4. **All pass** → invoice held in memory, approval request email sent to user (accept / decline)
5. **Any fail** → rejection email listing each failed check, invoice not stored

### `invoice_batch`
Triggered when an email contains multiple attachments (2–10 invoices).

- Each invoice is extracted and validated individually with per-invoice status tracking
- **All pass** → batch held in memory under a UUID key, single approval request covers all invoices
- **Any fail** → entire batch rejected with per-invoice pass/fail breakdown
- Accept/decline applies to the whole batch atomically

### `supplier_query`
Triggered when an email contains only a text body (no attachment).

1. LLM generates a read-only SELECT statement against the invoice schema
2. SQL is sanitised: blocked keywords (INSERT, UPDATE, DELETE, DROP, UNION, LOAD_FILE, etc.), stacked queries rejected, auto LIMIT 50
3. On SQL error, a retry prompt asks the LLM to fix the query
4. Results formatted into a structured reply (invoices, line items, supplier records, or aggregates)

### `combined_verification`
Triggered when an email contains both an attachment and a body. The frontend sends the invoice extraction first, then the supplier query sequentially — both replies appear in the same thread.

### `invoice_approval` / `batch_approval`
Triggered when the user replies "accept" or "decline" to a pending invoice or batch. Exact word matching only — any other reply prompts the user to decide.

- **Accept** → invoice(s) stored to MySQL (suppliers, clients, invoices, line_items tables)
- **Decline** → invoice(s) discarded, flagged for manual review

---

## Project Structure

```
invemailcheck/
├── backend/
│   ├── main.py                     # FastAPI entry point, rate limiting, CORS, validation
│   ├── agents/
│   │   ├── orchestrator.py         # LangGraph routing, LLM loading (4-bit NF4 + SDPA)
│   │   ├── invoice_agent.py        # DocLing OCR, LLM extraction, approval flow
│   │   ├── query_agent.py          # NL → SQL → DB → formatted reply
│   │   └── db.py                   # MySQL connection pool (SQLAlchemy)
│   └── pipelines/
│       ├── validation.py           # Required fields, maths, supplier, duplicate checks
│       └── email_draft.py          # All reply email templates (single + batch)
├── database/
│   └── init.sql                    # Schema — suppliers table
├── frontend/
│   ├── index.html                  # Email client UI (compose, threads, reply)
│   ├── script.js                   # State management, API calls, thread rendering
│   └── style.css                   # Dark industrial theme (IBM Plex Mono, amber accents)
├── notebooks/
│   └── 01_data_checks.ipynb        # DB inspection and data quality checks
├── .env                            # Environment variables (not committed)
├── .gitignore
├── LICENSE
└── README.md
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML / CSS / JS (Live Server) |
| Backend | FastAPI 0.135, Uvicorn, Python 3.12 |
| LLM | Llama 3.1 8B Instruct — 4-bit NF4 quantisation via bitsandbytes |
| LLM Framework | LangChain + HuggingFace Transformers |
| Routing | LangGraph (state machine: classify → agent → format) |
| OCR | DocLing + EasyOCR (CPU-only) |
| Database | MySQL, SQLAlchemy connection pool |
| GPU | AMD RX 7800 XT (ROCm / gfx1100) — also runs on NVIDIA CUDA |

### Key Design Decisions

- **4-bit NF4 quantisation** with double quantisation — fits the 8B model in ~4.5 GB VRAM
- **SDPA attention** (`attn_implementation="sdpa"`) — PyTorch native scaled dot-product attention for faster inference on ROCm
- **EasyOCR on CPU** — keeps GPU VRAM free for the LLM, avoids contention
- **Per-request AbortControllers** — concurrent frontend requests (e.g. query while invoice processes) don't cancel each other
- **Human-in-the-loop approval** — invoices are never auto-stored; the user must explicitly accept or decline
- **All-or-nothing batch validation** — if any invoice in a batch fails, the entire batch is rejected
- **SQL safety** — keyword blocklist, UNION rejection, stacked query rejection, auto LIMIT, parameterised queries for all DB writes

---

## Setup

### Prerequisites

- Python 3.12+
- MySQL running locally with the `invoice_db` database
- Llama 3.1 8B Instruct model downloaded locally
- AMD GPU with ROCm **or** NVIDIA GPU with CUDA

### Environment

Create a `.env` file in the project root:

```env
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_user
MYSQL_PASSWORD=your_password
MYSQL_DB=invoice_db

LLM_MODEL_PATH=/path/to/llama-3.1-8b-instruct
```

### Database

```bash
mysql -u your_user -p invoice_db < database/init.sql
```

### Install & Run

```bash
# Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install fastapi uvicorn sqlalchemy mysql-connector-python python-dotenv
pip install torch torchvision torchaudio          # or ROCm variant
pip install transformers accelerate bitsandbytes
pip install langchain langchain-huggingface langgraph
pip install docling easyocr

# Start backend
cd backend
uvicorn main:app --reload --port 8000

# Start frontend — open frontend/index.html with Live Server in VS Code
# Default: http://127.0.0.1:5500
```

For AMD GPUs, set these environment variables before starting the backend:

```bash
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export HSA_ENABLE_SDMA=0
```

---

## Security

- **CORS** — restricted to localhost origins only
- **Rate limiting** — 30 requests per minute per IP (in-memory)
- **Input validation** — MIME type allowlist (JPEG, PNG, TIFF, PDF), 25 MB size limit, pipeline name validation
- **SQL injection protection** — keyword blocklist (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, UNION, LOAD_FILE, etc.), stacked query rejection, read-only SELECT enforcement, auto LIMIT 50
- **Parameterised queries** — all database writes use SQLAlchemy parameterised statements
- **Error handling** — full stack traces logged server-side, safe generic messages returned to client

---

## License

[MIT](LICENSE) © James Scott