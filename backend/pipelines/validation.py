"""
validation.py — Invoice validation suite
"""

from __future__ import annotations
import os, sys

_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from agents.db import query as db_query


def run_all(ex: dict) -> list[dict]:
    return [
        _check_required_fields(ex),
        _check_maths(ex),
        _check_supplier(ex),
        _check_duplicate(ex),
    ]


def _check_maths(ex: dict) -> dict:
    try:
        items = ex.get("line_items") or []
        summary = ex.get("summary") or {}
        tol = 0.02
        line_net_total = 0.0

        for i, item in enumerate(items, 1):
            qty   = _f(item.get("qty"))
            price = _f(item.get("net_price"))
            worth = _f(item.get("net_worth"))
            if qty is not None and price is not None and worth is not None:
                expected = round(qty * price, 2)
                if abs(expected - worth) > tol:
                    return {"passed": False, "message":
                        f"Line {i}: {qty} × {price} = {expected} but net_worth shows {worth}."}
            if worth is not None:
                line_net_total += worth

        total_net   = _f(summary.get("total_net_worth"))
        total_vat   = _f(summary.get("total_vat"))
        total_gross = _f(summary.get("total_gross_worth"))

        if total_net is not None and abs(line_net_total - total_net) > tol:
            return {"passed": False, "message":
                f"Line items sum to {line_net_total:.2f} but summary shows {total_net:.2f}."}

        if total_net is not None and total_vat is not None and total_gross is not None:
            shipping = _f(ex.get("shipping"))
            expected_gross = round(total_net + total_vat + (shipping or 0.0), 2)
            if abs(expected_gross - total_gross) > tol:
                shipping_note = f" + {shipping:.2f} shipping" if shipping else ""
                return {"passed": False, "message":
                    f"{total_net:.2f} + {total_vat:.2f}{shipping_note} = {expected_gross:.2f} but gross shows {total_gross:.2f}."}

        return {"passed": True, "message": "Arithmetic checks passed."}
    except Exception as e:
        return {"passed": False, "message": f"Maths check error: {e}"}


def _check_supplier(ex: dict) -> dict:
    name = (ex.get("supplier") or {}).get("name")
    if not name:
        return {"passed": False, "message": "Supplier name missing from invoice."}
    rows = db_query("SELECT id FROM suppliers WHERE name = :name", {"name": name})
    if rows:
        return {"passed": True, "message": f"Supplier '{name}' found."}
    return {"passed": False, "message":
        f"Supplier '{name}' is not registered. Please register before resubmitting."}


def _check_duplicate(ex: dict) -> dict:
    inv_num = ex.get("invoice_number")
    if not inv_num:
        return {"passed": False, "message": "Invoice number missing."}
    rows = db_query("SELECT id FROM invoices WHERE invoice_number = :n", {"n": inv_num})
    if rows:
        return {"passed": False, "message": f"Invoice #{inv_num} has already been submitted."}
    return {"passed": True, "message": f"Invoice #{inv_num} is new."}


def _check_required_fields(ex: dict) -> dict:
    required = {
        "invoice_number":            ex.get("invoice_number"),
        "date_of_issue":             ex.get("date_of_issue"),
        "supplier.name":             (ex.get("supplier") or {}).get("name"),
        "summary.total_gross_worth": (ex.get("summary") or {}).get("total_gross_worth"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return {"passed": False, "message": f"Required fields missing: {', '.join(missing)}."}
    return {"passed": True, "message": "All required fields present."}


def _f(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace("$","").replace("£","").replace(",","").strip())
    except (ValueError, TypeError):
        return None