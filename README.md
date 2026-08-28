# Bank Statement Converter (self-hosted)

PDF bank statements → clean Excel / CSV. No AI, no API keys, no subscription, no upload.
Everything runs on your own machine.

---

## Setup (one time)

**Mac / Linux**
```bash
./run.sh
```

**Windows**
```
run.bat
```

That creates a virtual environment, installs the five dependencies, and opens the app at
<http://localhost:8501>.

Manual route if you prefer:
```bash
pip install -r requirements.txt
streamlit run app.py
```

---

## Terminal use (bulk jobs)

```bash
python convert.py statement.pdf                          # → statement-converted.xlsx
python convert.py inbox/*.pdf -o april.xlsx              # merge many PDFs into one sheet
python convert.py *.pdf --split                          # one output file per PDF
python convert.py locked.pdf --password 3105             # password-protected statement
python convert.py s.pdf --csv --flavour quickbooks       # QuickBooks-ready CSV
python convert.py us.pdf --month-first                   # US date order (01/31/2026)
python convert.py s.pdf --invert                         # if debit/credit come out swapped
```

Export flavours: `standard`, `quickbooks`, `xero`, `sage`.

---

## How it works

Two strategies, picked automatically per page:

1. **Column mode** — finds the header row (Date / Description / Debit / Credit / Balance),
   records each heading's x-position, then drops every word on the page into the column whose
   horizontal band it falls in. Accurate, and it survives wrapped descriptions and page breaks.
2. **Fallback mode** — no header row found. Any line starting with a date becomes a transaction;
   money tokens are stripped off the right-hand end; the running balance decides which amounts
   are money-in and which are money-out. Where the balance can't settle it (usually the first
   row), the wording of the description does.

Then every row is checked: `previous balance + amount == this balance`. Anything that doesn't tie
is listed in the **Check Failures** sheet of the Excel file and highlighted in the web UI. That
check is the difference between a converter you trust and one you have to eyeball line by line.

---

## Adding a new bank

Nine times out of ten you don't need to — the header detection handles it. When a bank uses
unusual wording, add it to `COLUMN_ALIASES` in `statement_parser.py`:

```python
COLUMN_ALIASES = {
    "date":        ["date", "trans date", "value date", ...],
    "description": ["description", "particulars", "narration", ...],
    "debit":       ["debit", "withdrawal", "money out", "paid out", ...],
    "credit":      ["credit", "deposit", "money in", "paid in", ...],
    "balance":     ["balance", "running balance", ...],
}
```

Add the exact heading text, lowercase. That's the whole customisation for most banks.

To see what the parser is actually reading:
```bash
python -c "import pdfplumber; print(pdfplumber.open('yourfile.pdf').pages[0].extract_text(layout=True))"
```

---

## Scanned statements

If a page has no text layer, the app reports it and skips it. Those need OCR:

```bash
sudo apt install ocrmypdf tesseract-ocr     # Mac: brew install ocrmypdf
ocrmypdf scanned.pdf searchable.pdf
python convert.py searchable.pdf
```

`ocrmypdf` adds an invisible text layer to the PDF and leaves the original image intact, so
the same parser then works normally. Always spot-check OCR'd figures — a smudged 8 becomes a 3.

---

## Hosting it for the team

The app is a plain Streamlit process, so on your VPS:

```bash
pip install -r requirements.txt
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Put it behind nginx with basic auth, or keep it on a Tailscale/VPN-only address. Client bank
statements are about as sensitive as documents get — don't leave it open to the internet, and
note that nothing is written to disk by the app itself (files live in memory for the request only).

---

## Files

| File | What it is |
|---|---|
| `statement_parser.py` | The engine — parsing, reconciliation, export |
| `app.py` | Streamlit drag-and-drop UI |
| `convert.py` | Command-line batch converter |
| `make_test_pdfs.py` | Generates three sample statements to test against |
| `run.sh` / `run.bat` | One-click launchers |

Test it end to end:
```bash
python make_test_pdfs.py && python convert.py test_a.pdf test_b.pdf test_c.pdf
```
All three layouts should report that every row reconciles.
