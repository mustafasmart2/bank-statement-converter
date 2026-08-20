"""Generate synthetic bank statement PDFs in several layouts, to test the parser."""
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4
TXNS = [
    ("01/04/2026", "OPENING BALANCE", None, None, 12500.00),
    ("03/04/2026", "CARD PAYMENT TO TESCO STORES 3245", 84.30, None, 12415.70),
    ("05/04/2026", "BACS CREDIT ACME LTD INVOICE 10023", None, 3200.00, 15615.70),
    ("07/04/2026", "DIRECT DEBIT BRITISH GAS", 142.55, None, 15473.15),
    ("09/04/2026", "FASTER PAYMENT TO J RAUF RENT APRIL", 1450.00, None, 14023.15),
    ("12/04/2026", "CARD PAYMENT AMAZON UK RETAIL", 219.99, None, 13803.16),
    ("15/04/2026", "BACS CREDIT HORIZON PARTNERS", None, 5750.25, 19553.41),
    ("18/04/2026", "STANDING ORDER HMRC PAYE 475XY", 890.00, None, 18663.41),
    ("22/04/2026", "CARD PAYMENT SHELL PETROL STATION", 65.40, None, 18598.01),
    ("28/04/2026", "BANK CHARGES MONTHLY FEE", 12.00, None, 18586.01),
]


def money(v):
    return f"{v:,.2f}" if v is not None else ""


def layout_a(path):
    """Classic UK layout: header row, right-aligned Debit / Credit / Balance columns."""
    c = canvas.Canvas(path, pagesize=A4)
    y = H - 60
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "NORTHGATE BANK PLC")
    c.setFont("Helvetica", 9)
    y -= 16
    c.drawString(50, y, "Statement of Account  |  Sort Code 20-45-11  |  Account Number 40028871")
    y -= 14
    c.drawString(50, y, "Statement period: 01 April 2026 to 30 April 2026")
    y -= 30

    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Date")
    c.drawString(110, y, "Description")
    c.drawRightString(400, y, "Debit")
    c.drawRightString(470, y, "Credit")
    c.drawRightString(545, y, "Balance")
    y -= 6
    c.line(50, y, 545, y)
    y -= 14

    c.setFont("Helvetica", 9)
    for date, desc, dr, cr, bal in TXNS:
        c.drawString(50, y, date)
        c.drawString(110, y, desc)
        c.drawRightString(400, y, money(dr))
        c.drawRightString(470, y, money(cr))
        c.drawRightString(545, y, money(bal))
        y -= 15
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(50, 40, "Page 1 of 1   Northgate Bank Plc is registered in England")
    c.save()


def layout_b(path):
    """Minimal layout: no header row, single signed amount + balance."""
    c = canvas.Canvas(path, pagesize=A4)
    y = H - 60
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, y, "MERIDIAN DIGITAL BANK")
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(50, y, "Transactions 01 Apr 2026 - 30 Apr 2026")
    y -= 28
    c.setFont("Helvetica", 9)
    for date, desc, dr, cr, bal in TXNS:
        amt = cr if cr else (dr if dr else None)
        if amt is None:
            continue
        c.drawString(50, y, date.replace("/04/2026", " Apr 2026"))
        c.drawString(150, y, desc.title())
        c.drawRightString(470, y, money(amt))
        c.drawRightString(545, y, money(bal))
        y -= 15
    c.save()


def layout_c(path):
    """Two pages + wrapped descriptions + noise lines."""
    c = canvas.Canvas(path, pagesize=A4)

    def header(y):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, y, "Transaction Date")
        c.drawString(150, y, "Particulars")
        c.drawRightString(420, y, "Withdrawal")
        c.drawRightString(487, y, "Deposit")
        c.drawRightString(558, y, "Balance")
        c.line(50, y - 6, 558, y - 6)
        c.setFont("Helvetica", 9)
        return y - 22

    y = H - 60
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, y, "UNITED COMMERCIAL BANK LIMITED")
    y -= 30
    y = header(y)
    for i, (date, desc, dr, cr, bal) in enumerate(TXNS):
        if i == 5:
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(50, 40, "Page 1 of 2 - continued")
            c.showPage()
            y = H - 60
            y = header(y)
        c.setFont("Helvetica", 9)
        c.drawString(50, y, date)
        c.drawString(150, y, desc[:38])
        c.drawRightString(420, y, money(dr))
        c.drawRightString(487, y, money(cr))
        c.drawRightString(558, y, money(bal))
        if len(desc) > 38:
            y -= 11
            c.setFont("Helvetica", 8)
            c.drawString(150, y, desc[38:])
        y -= 16
    c.save()


if __name__ == "__main__":
    layout_a("./test_a.pdf")
    layout_b("./test_b.pdf")
    layout_c("./test_c.pdf")
    print("wrote test_a.pdf test_b.pdf test_c.pdf")
