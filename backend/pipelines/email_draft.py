"""
email_draft.py — Builds the reply email body for the orchestrator.
"""

from __future__ import annotations


def _fmt(v) -> str:
    """Format a numeric value as a USD dollar amount, or '—' if absent."""
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def approved(sender: str, ex: dict) -> str:
    s     = ex.get("summary") or {}
    sup   = ex.get("supplier") or {}
    cli   = ex.get("client") or {}
    items = ex.get("line_items") or []

    SEP = "  " + "─" * 56

    lines = [
        f"Dear {sender},",
        "",
        "Your invoice has been received and processed successfully.",
        "",
        f"  INVOICE #  {ex.get('invoice_number', '—')}",
        f"  DATE       {ex.get('date_of_issue', '—')}",
        SEP,
        "",
        "  SUPPLIER",
        f"    Name     : {sup.get('name', '—')}",
        f"    Address  : {sup.get('address') or 'unknown'}",
        f"    Tax ID   : {sup.get('tax_id') or 'unknown'}",
        f"    IBAN     : {sup.get('iban') or 'unknown'}",
        "",
        "  CLIENT",
        f"    Name     : {cli.get('name') or 'unknown'}",
        f"    Address  : {cli.get('address') or 'unknown'}",
        f"    Tax ID   : {cli.get('tax_id') or 'unknown'}",
    ]

    if items:
        lines += [
            SEP,
            "",
            "  LINE ITEMS",
            f"  {'#':<4}  {'Description':<34}  {'Qty':>5}  {'Unit Price':>10}  {'Net Worth':>10}",
            "  " + "─" * 72,
        ]
        for i, item in enumerate(items, 1):
            desc = (item.get("description") or "—")
            if len(desc) > 32:
                desc = desc[:31] + "…"
            qty_v = item.get("qty")
            qty   = f"{float(qty_v):>5.1f}" if qty_v is not None else "    —"
            lines.append(
                f"  {i:<4}  {desc:<34}  {qty}"
                f"  {_fmt(item.get('net_price')):>10}"
                f"  {_fmt(item.get('net_worth')):>10}"
            )

    lines += [
        SEP,
        "",
        "  TOTALS",
    ]
    vat_pct  = ex.get("vat_percent")
    shipping = ex.get("shipping")
    lines.append(f"    Subtotal    : {_fmt(s.get('total_net_worth'))}")
    if vat_pct is not None:
        lines.append(f"    Sales Tax   : {_fmt(s.get('total_vat'))}  ({vat_pct}%)")
    else:
        lines.append(f"    Tax / VAT   : {_fmt(s.get('total_vat'))}")
    if shipping:
        lines.append(f"    Shipping    : {_fmt(shipping)}")
    lines += [
        f"    Gross Total : {_fmt(s.get('total_gross_worth'))}",
        "",
        SEP,
        "",
        "All validation checks passed. The invoice has been stored.",
        "",
        "Regards,",
        "AIMailbox",
    ]

    return "\n".join(lines)


def rejected(sender: str, ex: dict, failed_checks: list[dict]) -> str:
    inv_num = ex.get("invoice_number") or "unknown"
    sup     = (ex.get("supplier") or {}).get("name", "—")
    issues  = "\n".join(f"  • {c['message']}" for c in failed_checks)
    missing_hint = (
        "\nSome fields could not be read automatically from the document. "
        "Please review the items above and correct them manually before resubmitting, "
        "or contact us to arrange manual processing."
    )
    return (
        f"Dear {sender},\n\n"
        f"We were unable to process invoice {inv_num} from {sup}.\n"
        f"The following issues were found:\n\n"
        f"{issues}\n"
        f"{missing_hint}\n\n"
        f"Regards,\nAIMailbox"
    )


def approval_request(sender: str, ex: dict) -> str:
    """Rich review email sent before storing — asks user to accept or decline."""
    s     = ex.get("summary") or {}
    sup   = ex.get("supplier") or {}
    cli   = ex.get("client") or {}
    items = ex.get("line_items") or []
    vat_pct  = ex.get("vat_percent")
    shipping = ex.get("shipping")

    SEP = "  " + "─" * 56

    lines = [
        f"Dear {sender},",
        "",
        "The following invoice has been extracted and validated.",
        "Please review all details carefully.",
        "",
        f"  INVOICE #  {ex.get('invoice_number', '—')}",
        f"  DATE       {ex.get('date_of_issue', '—')}",
        SEP,
        "",
        "  SUPPLIER",
        f"    Name     : {sup.get('name', '—')}",
        f"    Address  : {sup.get('address') or 'unknown'}",
        f"    Tax ID   : {sup.get('tax_id') or 'unknown'}",
        f"    IBAN     : {sup.get('iban') or 'unknown'}",
        "",
        "  CLIENT",
        f"    Name     : {cli.get('name') or 'unknown'}",
        f"    Address  : {cli.get('address') or 'unknown'}",
        f"    Tax ID   : {cli.get('tax_id') or 'unknown'}",
    ]

    if items:
        lines += [
            SEP,
            "",
            "  LINE ITEMS",
            f"  {'#':<4}  {'Description':<34}  {'Qty':>5}  {'Unit Price':>10}  {'Net Worth':>10}",
            "  " + "─" * 72,
        ]
        for i, item in enumerate(items, 1):
            desc = (item.get("description") or "—")
            if len(desc) > 32:
                desc = desc[:31] + "…"
            qty_v = item.get("qty")
            qty   = f"{float(qty_v):>5.1f}" if qty_v is not None else "    —"
            lines.append(
                f"  {i:<4}  {desc:<34}  {qty}"
                f"  {_fmt(item.get('net_price')):>10}"
                f"  {_fmt(item.get('net_worth')):>10}"
            )

    lines += [SEP, "", "  TOTALS"]
    lines.append(f"    Subtotal    : {_fmt(s.get('total_net_worth'))}")
    if vat_pct is not None:
        lines.append(f"    Sales Tax   : {_fmt(s.get('total_vat'))}  ({vat_pct}%)")
    else:
        lines.append(f"    Tax / VAT   : {_fmt(s.get('total_vat'))}")
    if shipping:
        lines.append(f"    Shipping    : {_fmt(shipping)}")
    lines += [
        f"    Gross Total : {_fmt(s.get('total_gross_worth'))}",
        "",
        SEP,
        "",
        "  All validation checks passed.",
        "",
        "  ► To STORE this invoice to the payment queue, reply with ACCEPT.",
        "  ► To DISCARD and send to manual corrections, reply with DECLINE.",
        "",
        "Regards,",
        "AIMailbox",
    ]
    return "\n".join(lines)


def approval_confirmed(sender: str, invoice_num: str, ex: dict) -> str:
    sup = (ex.get("supplier") or {}).get("name", "—")
    s   = ex.get("summary") or {}
    return (
        f"Dear {sender},\n\n"
        f"Invoice {invoice_num} from {sup} has been accepted\n"
        f"and stored to the payment queue.\n\n"
        f"  Gross Total : {_fmt(s.get('total_gross_worth'))}\n\n"
        f"No further action required.\n\n"
        f"Regards,\nAIMailbox"
    )


def approval_declined(sender: str, invoice_num: str) -> str:
    return (
        f"Dear {sender},\n\n"
        f"Invoice {invoice_num} has been declined and will not be stored.\n\n"
        f"It has been flagged for manual review.\n"
        f"Please resubmit with any necessary corrections.\n\n"
        f"Regards,\nAIMailbox"
    )