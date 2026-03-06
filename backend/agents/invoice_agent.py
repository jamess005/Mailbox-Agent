"""
invoice_agent.py — DocLing extraction + validation coordinator
"""

from __future__ import annotations
import base64, os, sys, tempfile

# Ensure backend/ is on sys.path regardless of how this module is imported
_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from pipelines import validation, email_draft
from agents.db import execute, query as db_query

# In-memory store for invoices awaiting user accept/decline.
# Keyed by invoice_number. Entry removed once approved or declined.
_pending: dict[str, dict] = {}


def run(invoice: dict | None, email: dict, route: str, llm) -> dict:
    sender = email.get("from", "unknown")

    if invoice is None:
        return _err(sender, "No attachment found. Please resubmit with your invoice attached.")

    extracted = _extract(invoice, llm)
    if extracted.get("error"):
        return _err(sender, f"Could not read your invoice: {extracted['error']}")

    checks = validation.run_all(extracted)
    failed = [c for c in checks if not c["passed"]]

    if not failed:
        inv_num = extracted.get("invoice_number") or "unknown"
        _pending[inv_num] = extracted
        print(f"[invoice_agent] Pending approval stored for invoice #{inv_num}")
        return {
            "status":         "pending_approval",
            "invoice_number": inv_num,
            "email_body":     email_draft.approval_request(sender, extracted),
        }
    else:
        return {
            "status":        "rejected",
            "failed_checks": [c["message"] for c in failed],
            "email_body":    email_draft.rejected(sender, extracted, failed),
        }


_EXTRACTION_PROMPT = """\
Extract all invoice data from the text below. Reply with a single JSON object and nothing else.
Use null for any field not found. Numbers must be plain floats without currency symbols.
Note: OCR may have merged words or dropped spaces (e.g. "Cooper,Wilsonand Tran" means \
"Cooper, Wilson and Tran"). Use context to reconstruct correct spacing and names.

Required schema:
{{
  "invoice_number": "string",
  "date_of_issue": "YYYY-MM-DD or original string",
  "due_date": "YYYY-MM-DD or null",
  "seller_name": "string",
  "seller_address": "string or null",
  "seller_tax_id": "string or null",
  "seller_iban": "string or null",
  "buyer_name": "string",
  "buyer_address": "string or null",
  "buyer_tax_id": "string or null",
  "vat_percent": 8.0,
  "shipping": 0.0,
  "total_net": 0.0,
  "total_vat": 0.0,
  "total_gross": 0.0,
  "line_items": [
    {{"description": "string", "qty": 1.0, "net_price": 0.0, "net_worth": 0.0}}
  ]
}}

Invoice text:
{text}

JSON:"""


def _extract(invoice: dict, llm) -> dict:
    try:
        from docling.document_converter import (
            DocumentConverter, ImageFormatOption, PdfFormatOption,
        )
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, EasyOcrOptions
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice, AcceleratorOptions,
        )

        raw = base64.b64decode(invoice["base64_data"])
        ext = _mime_to_ext(invoice.get("mime_type", "image/jpeg"))
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(raw)
            path = f.name

        # EasyOCR handles English word-spacing far better than the Chinese
        # PP-OCRv4 model that RapidOCR uses by default.
        # CPU-only so it doesn’t compete with the LLM for VRAM.
        cpu = AcceleratorOptions(device=AcceleratorDevice.CPU)
        ocr_opts = EasyOcrOptions(lang=["en"], use_gpu=False)

        pdf_opts = PdfPipelineOptions()
        pdf_opts.accelerator_options = cpu
        pdf_opts.ocr_options = ocr_opts
        img_opts = PdfPipelineOptions()
        img_opts.accelerator_options = cpu
        img_opts.ocr_options = ocr_opts

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF:   PdfFormatOption(pipeline_options=pdf_opts),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=img_opts),
            }
        )
        result = converter.convert(path)
        os.unlink(path)
        markdown = result.document.export_to_markdown()
        parsed = _parse_fields(markdown, llm)
        if not parsed.get("error"):
            _log_extraction(parsed)
        return parsed

    except ImportError:
        return {"error": "DocLing not installed — run: pip install docling"}
    except Exception as e:
        return {"error": str(e)}


def _parse_fields(text: str, llm) -> dict:
    import json, re
    prompt = _EXTRACTION_PROMPT.format(text=text[:3000])
    # Strip any markdown code fences the model may add (```json ... ```)
    def _clean(s: str) -> str:
        s = re.sub(r'^```[\w]*\s*', '', s.strip())
        s = re.sub(r'```\s*$', '', s.strip())
        return s.strip()

    raw_out = llm.pipeline(prompt, max_new_tokens=1500, temperature=0.05,
                            do_sample=True, return_full_text=False)
    if not raw_out:
        return {"error": "LLM returned empty output."}
    # Transformers pipeline returns list of dicts OR list of strings depending on version
    first = raw_out[0]
    generated = first.get("generated_text", "") if isinstance(first, dict) else str(first)
    generated = _clean(generated)

    # Find the outermost {...} — use a non-greedy first-match approach so trailing
    # commentary after the JSON doesn't break parsing.
    match = re.search(r'\{[\s\S]+\}', generated)
    if not match:
        print(f"[invoice_agent] LLM returned no JSON. Raw output:\n{generated[:500]}")
        return {"error": f"LLM did not return JSON. Raw: {generated[:300]}"}    
    print(f"[invoice_agent] LLM JSON extracted ({len(match.group())} chars)")
    try:
        d = json.loads(match.group())
    except json.JSONDecodeError:
        # Try progressively trimming from the right to find valid JSON
        candidate = match.group()
        for i in range(len(candidate) - 1, 0, -1):
            if candidate[i] == '}':
                try:
                    d = json.loads(candidate[:i + 1])
                    break
                except json.JSONDecodeError:
                    continue
        else:
            return {"error": f"Could not parse JSON from LLM output. Raw: {generated[:300]}"}

    def _s(v): return str(v).strip() if v not in (None, "") else None
    def _f(v):
        if v is None: return None
        try: return float(str(v).replace(",", ".").replace("$", "").replace("£", "").strip())
        except (ValueError, TypeError): return None

    line_items = []
    for item in (d.get("line_items") or []):
        line_items.append({
            "qty":       _f(item.get("qty")),
            "net_price": _f(item.get("net_price")),
            "net_worth": _f(item.get("net_worth")),
            "description": item.get("description"),
        })

    return {
        "invoice_number": _s(d.get("invoice_number")),
        "date_of_issue":  _s(d.get("date_of_issue")),
        "due_date":       _s(d.get("due_date")),
        "supplier": {
            "name":    _s(d.get("seller_name")),
            "address": _s(d.get("seller_address")),
            "tax_id":  _s(d.get("seller_tax_id")),
            "iban":    _s(d.get("seller_iban")),
        },
        "client": {
            "name":    _s(d.get("buyer_name")),
            "address": _s(d.get("buyer_address")),
            "tax_id":  _s(d.get("buyer_tax_id")),
        },
        "vat_percent": _f(d.get("vat_percent")),
        "shipping":    _f(d.get("shipping")),
        "line_items": line_items,
        "summary": _build_summary(
            _f(d.get("total_net")),
            _f(d.get("total_vat")),
            _f(d.get("total_gross")),
            _f(d.get("shipping")),
        ),
    }


def _build_summary(net, vat, gross, shipping) -> dict:
    """Return a summary dict, recalculating gross = net + vat + shipping when shipping is present."""
    if net is not None and vat is not None and shipping is not None:
        correct_gross = round(net + vat + shipping, 2)
        # Override whatever the LLM said if shipping wasn't baked in
        gross = correct_gross
    return {
        "total_net_worth":   net,
        "total_vat":         vat,
        "total_gross_worth": gross,
    }


def _log_extraction(ex: dict):
    sup   = ex.get("supplier") or {}
    cli   = ex.get("client") or {}
    s     = ex.get("summary") or {}
    items = ex.get("line_items") or []
    sep   = "  " + "─" * 54
    print(f"[invoice_agent] ── Extraction result ──────────────────────────")
    print(f"  Invoice #  : {ex.get('invoice_number')}   Date: {ex.get('date_of_issue')}")
    print(sep)
    print(f"  Supplier   : {sup.get('name')}")
    print(f"  Address    : {sup.get('address')}")
    print(f"  Tax ID     : {sup.get('tax_id')}   IBAN: {sup.get('iban')}")
    print(sep)
    print(f"  Client     : {cli.get('name')}")
    print(f"  Address    : {cli.get('address')}")
    print(f"  Tax ID     : {cli.get('tax_id')}")
    print(sep)
    print(f"  Line items : {len(items)}")
    for i, item in enumerate(items, 1):
        desc = (item.get("description") or "—")[:50]
        print(f"    {i:>2}. {desc:<50}  qty={item.get('qty')}  "
              f"price={item.get('net_price')}  worth={item.get('net_worth')}")
    print(sep)
    vat_pct  = ex.get("vat_percent")
    shipping = ex.get("shipping")
    tax_label = f"Tax({vat_pct}%): " if vat_pct is not None else "VAT: "
    print(f"  Net: {s.get('total_net_worth')}   {tax_label}{s.get('total_vat')}   "
          f"Shipping: {shipping}   Gross: {s.get('total_gross_worth')}")
    print(f"[invoice_agent] ────────────────────────────────────────────────")


def _parse_date(raw: str | None) -> str | None:
    """Try common date formats and return YYYY-MM-DD for MySQL, or None."""
    if not raw:
        return None
    import re
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
                "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
                "%m/%d/%y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Last-chance: strip non-digit separators and try
    cleaned = re.sub(r"[^\d]", "-", raw.strip())
    for fmt in ("%m-%d-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None  # Let MySQL store NULL rather than crashing


def _store(ex: dict):
    import json as _json

    sup = ex.get("supplier") or {}
    execute(
        """INSERT INTO suppliers (name, address, tax_id, iban)
           VALUES (:name, :address, :tax_id, :iban)
           ON DUPLICATE KEY UPDATE address=VALUES(address), iban=VALUES(iban)""",
        sup,
    )
    sup_id = (db_query(
        "SELECT id FROM suppliers WHERE name=:name", {"name": sup.get("name")}
    ) or [{}])[0].get("id")

    cli = ex.get("client") or {}
    execute(
        """INSERT INTO clients (name, address, tax_id)
           VALUES (:name, :address, :tax_id)
           ON DUPLICATE KEY UPDATE address=VALUES(address)""",
        cli,
    )
    cli_id = (db_query(
        "SELECT id FROM clients WHERE name=:name", {"name": cli.get("name")}
    ) or [{}])[0].get("id")

    s = ex.get("summary") or {}
    execute(
        """INSERT INTO invoices
               (invoice_number, date_of_issue,
                supplier_id, client_id,
                total_net_worth, vat_percent, total_vat_amount, total_gross_worth,
                raw_data)
           VALUES
               (:invoice_number, :date_of_issue,
                :supplier_id, :client_id,
                :total_net_worth, :vat_percent, :total_vat_amount, :total_gross_worth,
                :raw_data)""",
        {
            "invoice_number":    ex.get("invoice_number"),
            "date_of_issue":     _parse_date(ex.get("date_of_issue")),
            "supplier_id":       sup_id,
            "client_id":         cli_id,
            "total_net_worth":   s.get("total_net_worth"),
            "vat_percent":       ex.get("vat_percent"),
            "total_vat_amount":  s.get("total_vat"),
            "total_gross_worth": s.get("total_gross_worth"),
            "raw_data":          _json.dumps(ex),
        },
    )

    inv_id = (db_query(
        "SELECT id FROM invoices WHERE invoice_number=:n ORDER BY id DESC LIMIT 1",
        {"n": ex.get("invoice_number")},
    ) or [{}])[0].get("id")

    for i, item in enumerate(ex.get("line_items") or [], 1):
        execute(
            """INSERT INTO line_items
                   (invoice_id, item_number, description, quantity, net_price, net_worth)
               VALUES
                   (:invoice_id, :item_number, :description, :quantity, :net_price, :net_worth)""",
            {
                "invoice_id":  inv_id,
                "item_number": i,
                "description": item.get("description"),
                "quantity":    item.get("qty"),
                "net_price":   item.get("net_price"),
                "net_worth":   item.get("net_worth"),
            },
        )


def run_approval(invoice_number: str, decision: str, sender: str) -> dict:
    """Process an accept/decline reply for a pending extracted invoice."""
    ex = _pending.pop(invoice_number, None)
    if ex is None:
        return {
            "status":     "error",
            "email_body": (
                f"Dear {sender},\n\n"
                f"Could not find a pending invoice with reference #{invoice_number}.\n"
                f"It may have already been processed, or the server may have restarted.\n"
                f"Please resubmit the original invoice if needed.\n\n"
                f"Regards,\nAIMailbox"
            ),
        }
    if decision == "accept":
        _store(ex)
        print(f"[invoice_agent] Invoice #{invoice_number} ACCEPTED and stored.")
        return {
            "status":         "approved",
            "invoice_number": invoice_number,
            "email_body":     email_draft.approval_confirmed(sender, invoice_number, ex),
        }
    else:
        print(f"[invoice_agent] Invoice #{invoice_number} DECLINED — not stored.")
        return {
            "status":         "declined",
            "invoice_number": invoice_number,
            "email_body":     email_draft.approval_declined(sender, invoice_number),
        }


def _err(sender: str, msg: str) -> dict:
    return {"status": "error", "email_body": f"Dear {sender},\n\n{msg}\n\nRegards,\nAIMailbox"}


def _mime_to_ext(mime: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png",
            "image/tiff": ".tiff", "application/pdf": ".pdf"}.get(mime, ".jpg")