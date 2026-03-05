# Mailbox-Agent

An autonomous invoice and supplier query processing pipeline built around a simulated email mailbox. Incoming emails are routed to one of three pipelines based on content — invoice extraction, supplier database query, or combined verification — and a reply is drafted and returned to the sender automatically.

[![LinkedIn](https://img.shields.io/badge/LinkedIn-James%20Scott-0077B5?logo=linkedin)](https://www.linkedin.com/in/jamesscott005)

---

## Overview

The system simulates a dedicated mailbox (`AIMailbox@gmail.com`) that receives:

- **Invoice attachments** — extracted via DocLing, validated against the database, and checked for mathematical consistency, supplier registration, duplicate detection, and anomalous line-item pricing
- **Supplier / database queries** — parsed by an LLM that generates and executes SQL against the invoices MySQL database, returning results as a reply email
- **Combined emails** — attachment extraction and query resolution run together

All results are returned as email replies rendered in the frontend thread view.

---

## Architecture

```
Frontend (HTML/CSS/JS — Live Server)
  │
  └─ POST /api/process
        │
        ├─ orchestrator.py          — routes to correct pipeline
        │
        ├─ invoice_extraction       — DocLing → field extraction → validation suite
        │     ├─ maths checks       — line totals, VAT, gross
        │     ├─ supplier check     — registered in DB?
        │     ├─ duplicate check    — invoice number seen before?
        │     ├─ anomaly detection  — prices / totals within historical range?
        │     └─ email_draft.py     — rejection or confirmation email
        │
        └─ supplier_query           — LLM reads query → generates SQL → queries DB → reply
```

---

## Pipelines

### `invoice_extraction`
Triggered when an email contains an invoice attachment (PNG, JPG, PDF, TIFF).

1. DocLing extracts structured fields: invoice number, date, due date, supplier, client, line items, totals
2. Validation suite runs:
   - **Maths** — line item totals, VAT consistency, gross = net + VAT
   - **Supplier** — is the supplier registered in the `suppliers` table?
   - **Duplicate** — has this invoice number been seen before?
   - **Anomaly** — are prices and totals within historical range for this supplier?
3. **Pass** → store to DB, send confirmation reply
4. **Fail** → draft rejection email listing each failed check with calculated correct values where possible, return to sender without storing

### `supplier_query`
Triggered when an email contains only a body (no attachment).

1. LLM reads the query and classifies intent (supplier lookup, amount check, duplicate check, general)
2. LLM generates SQL against the invoices schema
3. SQL executes against MySQL
4. Results formatted into a plain-text reply email

### `combined_verification`
Triggered when an email contains both a body and an attachment. Runs both pipelines and merges the reply.

---

## Project Structure

```
invemailcheck/
├── backend/
│   ├── main.py                     # FastAPI app — POST /api/process
│   ├── db.py                       # MySQL connection via .env credentials
│   ├── agents/
│   │   ├── orchestrator.py         # Routes payload to correct pipeline
│   │   ├── invoice_agent.py        # DocLing extraction + validation coordinator
│   │   └── query_agent.py          # LLM SQL generation + DB query
│   ├── pipelines/
│   │   ├── extraction.py           # DocLing field extraction
│   │   ├── validation.py           # Maths, supplier, duplicate, anomaly checks
│   │   ├── anomaly.py              # Historical range checks
│   │   └── email_draft.py          # Reply email builder
│   └── utils/
│       └── audit.py                # Operator audit logging for financial edits
├── database/
│   └── init.sql                    # Schema — invoices, suppliers, clients, line_items
├── frontend/
│   ├── index.html                  # Email client UI
│   ├── script.js                   # Compose, send, thread rendering
│   └── style.css                   # Dark industrial theme
└── notebooks/
    └── 01_data_checks.ipynb        # DB inspection and data quality checks
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML / CSS / JS (Live Server) |
| Backend | FastAPI, Uvicorn, Python 3.12 |
| Invoice extraction | DocLing |
| LLM inference | Llama 3.1 8B Instruct (local, via HuggingFace transformers) |
| Database | MySQL |
| ORM / queries | SQLAlchemy, mysql-connector-python |
| Environment | python-dotenv |

---

## Setup

### Prerequisites

- Python 3.12
- MySQL running locally with the schema from `database/init.sql`
- `.env` file one level above the project root (see below)
- Llama 3.1 8B Instruct model at `~/ml-proj/models/llama-3.1-8b-instruct/`
- DocLing installed (`pip install docling`)

### `.env`

```
MYSQL_HOST=localhost
MYSQL_USER=your_user
MYSQL_PASSWORD=your_password
MYSQL_DB=your_database

LLM_MODEL_PATH=/home/james/ml-proj/models/llama-3.1-8b-instruct/snapshots/0e9e39f249a16976918f6564b8830bc894c89659
EMBEDDING_MODEL_PATH=/home/james/ml-proj/models/all-mpnet-base-v2/snapshots/e8c3b32edf5434bc2275fc9bab85f82640a19130
```

### Run

```bash
# Backend
cd invemailcheck/backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend
# Open frontend/index.html with Live Server in VS Code
# Default: http://127.0.0.1:5500
```

---

## Audit Logging

All manual edits to extracted invoice fields are logged with operator ID, timestamp, old value, and new value. Financial field edits (net prices, totals, VAT) are flagged separately in the audit trail. The approved payload sent to the backend carries the full audit log so any pre-approval manipulation is traceable.

---

## License

[MIT](LICENSE) © James Scott