"""Generate synthetic invoice PDFs that mimic real Indian GST invoice layouts.

Deliberately varies line-item counts so that totals move down the page - the
exact condition under which fixed-rectangle extraction fails.
"""
import sys
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

W, H = A4

VENDORS = {
    "acme": dict(name="ACME INDUSTRIES PVT LTD", gstin="29AABCA1234A1ZF",
                 addr="Plot 14, Industrial Area, Bengaluru 560058", prefix="ACM"),
    "borex": dict(name="BOREX TRADING COMPANY", gstin="27AAECB5678K1Z6",
                  addr="221 Marine Lines, Mumbai 400002", prefix="BX"),
}


def inr(v):
    """Indian digit grouping: 1,23,456.00"""
    s = f"{v:.2f}"
    whole, frac = s.split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + "." + frac


def make_invoice(path, vendor_key, invoice_no, date_str, po_no, items,
                 tax_rate=Decimal("18")):
    v = VENDORS[vendor_key]
    c = canvas.Canvas(str(path), pagesize=A4)

    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, H - 22 * mm, v["name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, H - 27 * mm, v["addr"])
    c.drawString(20 * mm, H - 31 * mm, f"GSTIN: {v['gstin']}")

    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(W / 2, H - 42 * mm, "TAX INVOICE")

    y = H - 54 * mm
    c.setFont("Helvetica", 9.5)
    for label, value in [("Invoice No.", invoice_no), ("Invoice Date", date_str),
                         ("PO Number", po_no)]:
        c.drawString(115 * mm, y, f"{label} :")
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(150 * mm, y, str(value))
        c.setFont("Helvetica", 9.5)
        y -= 6 * mm

    c.drawString(20 * mm, H - 54 * mm, "Bill To :")
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(20 * mm, H - 59 * mm, "NORTHWIND LOGISTICS LLP")
    c.setFont("Helvetica", 9.5)
    c.drawString(20 * mm, H - 64 * mm, "GSTIN: 29AAGCN9012P1ZU")

    top = H - 80 * mm
    cols = [20, 32, 92, 112, 128, 152, 190]   # mm boundaries
    headers = ["Sr", "Description", "HSN", "Qty", "Rate", "Amount"]
    c.setFont("Helvetica-Bold", 9)
    for i, htxt in enumerate(headers):
        if i in (3, 4, 5):
            c.drawRightString(cols[i + 1] * mm - 2 * mm, top, htxt)
        else:
            c.drawString(cols[i] * mm + 1.5 * mm, top, htxt)
    c.setLineWidth(0.5)
    c.line(cols[0] * mm, top - 2 * mm, cols[-1] * mm, top - 2 * mm)
    c.line(cols[0] * mm, top + 4 * mm, cols[-1] * mm, top + 4 * mm)

    y = top - 8 * mm
    c.setFont("Helvetica", 8.5)
    subtotal = Decimal("0")
    for idx, (desc, hsn, qty, rate) in enumerate(items, start=1):
        amount = (Decimal(qty) * Decimal(rate)).quantize(Decimal("0.01"))
        subtotal += amount
        c.drawString(cols[0] * mm + 1.5 * mm, y, str(idx))
        c.drawString(cols[1] * mm + 1.5 * mm, y, desc[:44])
        c.drawString(cols[2] * mm + 1.5 * mm, y, hsn)
        c.drawRightString(cols[4] * mm - 2 * mm, y, str(qty))
        c.drawRightString(cols[5] * mm - 2 * mm, y, inr(Decimal(rate)))
        c.drawRightString(cols[6] * mm - 2 * mm, y, inr(amount))
        y -= 5.5 * mm

    y -= 3 * mm
    c.line(cols[0] * mm, y + 2 * mm, cols[-1] * mm, y + 2 * mm)
    tax = (subtotal * tax_rate / 100).quantize(Decimal("0.01"))
    grand = subtotal + tax
    c.setFont("Helvetica", 9)
    for label, value in [("Sub Total", subtotal),
                         (f"IGST {tax_rate}%", tax), ("Grand Total", grand)]:
        if label == "Grand Total":
            c.setFont("Helvetica-Bold", 10)
        c.drawRightString(cols[5] * mm, y, label)
        c.drawRightString(cols[6] * mm - 2 * mm, y, "Rs. " + inr(value))
        y -= 6 * mm

    c.setFont("Helvetica", 7.5)
    c.drawString(20 * mm, 18 * mm, "Terms: Payment due within 30 days.")
    c.save()
    return dict(invoice_no=invoice_no, date=date_str, po=po_no,
                subtotal=subtotal, tax=tax, grand=grand, lines=len(items))


BASE_ITEMS = [
    ("M8 Hex Bolt Zinc Plated", "73181500", 250, "12.50"),
    ("Steel Washer 8mm", "73182200", 500, "2.75"),
    ("Nylon Lock Nut M8", "73181600", 250, "4.20"),
    ("Threadlocker Adhesive 50ml", "35061000", 12, "340.00"),
    ("Assorted Fastener Kit - workshop grade", "73181500", 4, "1250.00"),
    ("Anti-seize Compound 250g", "34031900", 6, "480.00"),
    ("Hex Key Set 1.5-10mm", "82041100", 8, "395.00"),
]

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures")
    out.mkdir(parents=True, exist_ok=True)
    made = []
    # Varying line counts: this is what breaks fixed-coordinate extraction.
    for i, n in enumerate([3, 7, 2, 5, 4], start=1):
        made.append(make_invoice(out / f"acme_invoice_{i:03d}.pdf", "acme",
                                 f"ACM/2024/{870 + i:05d}",
                                 f"{10 + i:02d}/03/2024",
                                 f"PO-NW-{4400 + i}", BASE_ITEMS[:n]))
    for i, n in enumerate([4, 6], start=1):
        made.append(make_invoice(out / f"borex_invoice_{i:03d}.pdf", "borex",
                                 f"BX-2024-{1200 + i}",
                                 f"{20 + i:02d}/04/2024",
                                 f"PO-NW-{5500 + i}", BASE_ITEMS[:n]))
    for m in made:
        print(m)
    print(f"\nwrote {len(made)} PDFs to {out}")
