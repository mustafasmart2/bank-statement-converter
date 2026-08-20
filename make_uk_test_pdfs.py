"""
Approximations of the layouts used by the main UK banks, for regression testing.

These are built from the common shape of each bank's PDF statement, not from real
customer documents. They exercise the parser's handling of each wording style;
they are not a guarantee that a real statement from that bank will match.
"""
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4

TXNS = [
    ("03", "04", "CARD PAYMENT TO TESCO STORES 3245", -84.30, 12415.70),
    ("05", "04", "BACS CREDIT ACME LTD INVOICE 10023", 3200.00, 15615.70),
    ("07", "04", "DIRECT DEBIT BRITISH GAS", -142.55, 15473.15),
    ("09", "04", "FASTER PAYMENT J RAUF RENT APRIL", -1450.00, 14023.15),
    ("12", "04", "CARD PAYMENT AMAZON UK RETAIL", -219.99, 13803.16),
    ("15", "04", "BACS CREDIT HORIZON PARTNERS LLP", 5750.25, 19553.41),
    ("18", "04", "STANDING ORDER HMRC PAYE 475XY", -890.00, 18663.41),
    ("22", "04", "CARD PAYMENT SHELL PETROL STATION", -65.40, 18598.01),
    ("28", "04", "SERVICE CHARGE MONTHLY FEE", -12.00, 18586.01),
]
OPENING = 12500.00
MONTHS = {"04": "Apr"}


def m(v):
    return f"{v:,.2f}" if v is not None else ""


def _two_col_bank(path, bank, out_hdr, in_hdr, bal_hdr, date_hdr, desc_hdr,
                  dated=lambda d, mo: f"{d}/{mo}/2026", period=None):
    """Generic UK two-money-column statement."""
    c = canvas.Canvas(path, pagesize=A4)
    y = H - 55
    c.setFont("Helvetica-Bold", 15)
    c.drawString(45, y, bank)
    c.setFont("Helvetica", 8.5)
    y -= 15
    c.drawString(45, y, period or "Your statement 1 April 2026 to 30 April 2026")
    y -= 12
    c.drawString(45, y, "Account 40028871   Sort code 20-45-11")
    y -= 26

    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(45, y, date_hdr)
    c.drawString(120, y, desc_hdr)
    c.drawRightString(400, y, out_hdr)
    c.drawRightString(468, y, in_hdr)
    c.drawRightString(552, y, bal_hdr)
    c.line(45, y - 5, 552, y - 5)
    y -= 20

    c.setFont("Helvetica", 8.5)
    c.drawString(45, y, dated("01", "04"))
    c.drawString(120, y, "BALANCE BROUGHT FORWARD")
    c.drawRightString(552, y, m(OPENING))
    y -= 14

    for d, mo, desc, amt, bal in TXNS:
        c.drawString(45, y, dated(d, mo))
        c.drawString(120, y, desc)
        c.drawRightString(400, y, m(-amt) if amt < 0 else "")
        c.drawRightString(468, y, m(amt) if amt > 0 else "")
        c.drawRightString(552, y, m(bal))
        y -= 14
    c.save()


def barclays(path):
    _two_col_bank(path, "Barclays Bank UK PLC", "Money out", "Money in", "Balance",
                  "Date", "Description")


def hsbc(path):
    """HSBC prints day + short month with no year; the year is only in the period line."""
    _two_col_bank(path, "HSBC UK Bank plc", "Paid out", "Paid in", "Balance",
                  "Date", "Payment type and details",
                  dated=lambda d, mo: f"{int(d)} {MONTHS[mo]}",
                  period="Statement period 01 April 2026 to 30 April 2026")


def lloyds(path):
    _two_col_bank(path, "Lloyds Bank plc", "Paid out", "Paid in", "Balance",
                  "Date", "Payment type and details")


def natwest(path):
    _two_col_bank(path, "National Westminster Bank Plc", "Withdrawn", "Paid in",
                  "Balance", "Date", "Type Description")


def monzo(path):
    """Challenger banks tend to use a single signed Amount column."""
    c = canvas.Canvas(path, pagesize=A4)
    y = H - 55
    c.setFont("Helvetica-Bold", 15)
    c.drawString(45, y, "Monzo Bank Limited")
    c.setFont("Helvetica", 8.5)
    y -= 15
    c.drawString(45, y, "Statement for 1 April 2026 - 30 April 2026")
    y -= 28
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(45, y, "Date")
    c.drawString(120, y, "Description")
    c.drawRightString(460, y, "Amount")
    c.drawRightString(552, y, "Balance")
    c.line(45, y - 5, 552, y - 5)
    y -= 20
    c.setFont("Helvetica", 8.5)
    for d, mo, desc, amt, bal in TXNS:
        c.drawString(45, y, f"{d}/{mo}/2026")
        c.drawString(120, y, desc.title())
        c.drawRightString(460, y, m(amt))
        c.drawRightString(552, y, m(bal))
        y -= 14
    c.save()


def santander(path):
    """Santander UK uses labelled blocks rather than a table."""
    c = canvas.Canvas(path, pagesize=A4)
    y = H - 55
    c.setFont("Helvetica-Bold", 15)
    c.drawString(45, y, "Santander UK plc")
    c.setFont("Helvetica", 8.5)
    y -= 15
    c.drawString(45, y, "Statement period: 01/04/2026 to 30/04/2026")
    y -= 28
    for d, mo, desc, amt, bal in TXNS:
        if y < 90:
            c.showPage()
            y = H - 55
            c.setFont("Helvetica", 8.5)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(45, y, "Date:")
        c.setFont("Helvetica", 8.5)
        c.drawString(110, y, f"{d}/{mo}/2026")
        y -= 12
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(45, y, "Description:")
        c.setFont("Helvetica", 8.5)
        c.drawString(110, y, desc)
        y -= 12
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(45, y, "Amount:")
        c.setFont("Helvetica", 8.5)
        c.drawString(110, y, m(amt))
        y -= 12
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(45, y, "Balance:")
        c.setFont("Helvetica", 8.5)
        c.drawString(110, y, m(bal))
        y -= 20
    c.save()


BUILDERS = {
    "uk_barclays.pdf": barclays,
    "uk_hsbc.pdf": hsbc,
    "uk_lloyds.pdf": lloyds,
    "uk_natwest.pdf": natwest,
    "uk_monzo.pdf": monzo,
    "uk_santander.pdf": santander,
}

if __name__ == "__main__":
    for name, fn in BUILDERS.items():
        fn(name)
        print("wrote", name)
