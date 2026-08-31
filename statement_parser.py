"""
statement_parser.py
-------------------
Bank statement PDF -> structured transactions.

No AI, no API, no subscription. Pure layout parsing.

Strategy (in order):
  1. Column mode  - find the header row (Date / Description / Debit / Credit / Balance),
                    record each column's x-range, then bucket every word on the page
                    into the correct column. Most accurate, works for most banks.
  2. Fallback mode- no header found: treat any line starting with a date as a
                    transaction, pull the trailing numbers off the end, and use the
                    running balance to decide what is money-in vs money-out.

Both modes handle multi-line descriptions and multi-page statements.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pdfplumber
import pandas as pd

# --------------------------------------------------------------------------
# 1. Date handling
# --------------------------------------------------------------------------

DATE_PATTERNS: list[tuple[str, list[str]]] = [
    (r"\d{1,2}[/-]\d{1,2}[/-]\d{4}", ["%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"]),
    (r"\d{1,2}[/-]\d{1,2}[/-]\d{2}\b", ["%d/%m/%y", "%m/%d/%y", "%d-%m-%y", "%m-%d-%y"]),
    (r"\d{4}-\d{2}-\d{2}", ["%Y-%m-%d"]),
    (r"\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{4}", ["%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%B-%Y"]),
    (r"\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2}\b", ["%d %b %y", "%d-%b-%y"]),
    (r"[A-Za-z]{3,9} \d{1,2},? \d{4}", ["%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"]),
    (r"\d{1,2}[ -][A-Za-z]{3,9}\b", ["%d %b", "%d-%b"]),  # year-less (e.g. HSBC)
]

DATE_RE = re.compile("^(" + "|".join(p for p, _ in DATE_PATTERNS) + r")\b")


YEAR_IN_DATE_RE = re.compile(r"\b\d{4}\b|[/-]\d{2}$|\s\d{2}$")


def date_has_year(raw: str) -> bool:
    return bool(raw) and bool(YEAR_IN_DATE_RE.search(raw.strip()))


def parse_date(text: str, dayfirst: bool = True, default_year: int | None = None):
    """Return (datetime|None, matched_string|None)."""
    text = text.strip()
    m = DATE_RE.match(text)
    if not m:
        return None, None
    raw = m.group(1)
    for pattern, formats in DATE_PATTERNS:
        if not re.fullmatch(pattern, raw):
            continue
        fmts = formats if dayfirst else list(reversed(formats))
        for fmt in fmts:
            try:
                dt = datetime.strptime(raw.replace(",", ""), fmt.replace(",", ""))
                if dt.year == 1900 and default_year:
                    dt = dt.replace(year=default_year)
                return dt, raw
            except ValueError:
                continue
    return None, raw


# --------------------------------------------------------------------------
# 2. Amount handling
# --------------------------------------------------------------------------

# 1,234.56 | 1.234,56 | (1,234.56) | 1234.56- | -1234.56 | 1,234.56 DR
AMOUNT_RE = re.compile(
    r"""(?<![\w/])
        (?P<open>\()?
        (?P<sign>[-+])?
        (?P<cur>[$£€₹]|Rs\.?|AED|USD|GBP|PKR|CAD)?\s?
        (?P<num>\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)
        (?P<close>\))?
        \s?(?P<drcr>DR|CR|Dr|Cr)?
        (?P<trail>-)?
        (?![\w/])""",
    re.VERBOSE,
)


def to_number(token: str) -> float | None:
    """Parse a single money token into a signed float."""
    m = AMOUNT_RE.fullmatch(token.strip())
    if not m:
        m = AMOUNT_RE.search(token.strip())
        if not m:
            return None
    num = m.group("num").replace(",", "").replace(" ", "")
    try:
        val = float(num)
    except ValueError:
        return None
    negative = bool(m.group("open")) or m.group("sign") == "-" or bool(m.group("trail"))
    if (m.group("drcr") or "").upper() == "DR":
        negative = True
    return -val if negative else val


# A real money token has decimals or thousands separators. A bare run of digits
# (2345, 10023) is almost always a cheque number / reference, not an amount.
FORMATTED_MONEY_RE = re.compile(
    r"^[\(\-+]?\s?[$£€₹]?\s?(?:Rs\.?\s?)?"
    r"(?:\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2})"
    r"\)?\s?(?:DR|CR|Dr|Cr)?-?$"
)


def looks_like_amount(token: str, strict: bool = True) -> bool:
    t = token.strip()
    if not t or not any(c.isdigit() for c in t):
        return False
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?", t):  # a date, not money
        return False
    if strict:
        return FORMATTED_MONEY_RE.match(t) is not None
    return AMOUNT_RE.fullmatch(t) is not None


def trailing_amounts(line: str, max_n: int = 4) -> tuple[str, list[float]]:
    """Strip money tokens off the right-hand end. Returns (remaining_text, values)."""
    tokens = line.split()
    values: list[float] = []
    while tokens and len(values) < max_n and looks_like_amount(tokens[-1], strict=True):
        tok = tokens.pop()
        # glue a currency symbol that got split off, e.g. "£" "1,200.00"
        if tokens and tokens[-1] in {"$", "£", "€", "₹", "Rs", "Rs.", "-"}:
            tok = tokens.pop() + tok
        v = to_number(tok)
        if v is None:
            break
        values.insert(0, v)
    return " ".join(tokens), values


# --------------------------------------------------------------------------
# 3. Column detection
# --------------------------------------------------------------------------

# Headings seen across UK high-street banks, challenger banks and the common
# international layouts. Everything is matched lowercase, punctuation stripped.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": [
        "date", "trans date", "transaction date", "value date", "posting date",
        "posted", "date of transaction", "trans", "txn date", "book date",
        "entry date", "tarikh",
    ],
    "description": [
        "description", "particulars", "details", "narrative", "transaction",
        "transaction details", "payment type and details", "merchant",
        "payee", "memo", "narration", "reference", "your reference",
        "payment details", "counterparty", "activity", "what",
    ],
    "debit": [
        # UK wording
        "money out", "paid out", "payments out", "withdrawn", "out", "out £",
        "money out £", "paid out £", "payments", "withdrawals", "withdrawal",
        # generic
        "debit", "debits", "dr", "outflow", "charges", "spent", "expense",
    ],
    "credit": [
        # UK wording
        "money in", "paid in", "payments in", "in", "in £", "money in £",
        "paid in £", "receipts", "deposits", "deposit",
        # generic
        "credit", "credits", "cr", "inflow", "received",
    ],
    "code": ["type", "transaction type", "tran type", "code", "trans type"],
    "amount": ["amount", "amount £", "value", "transaction amount", "amt", "amount (gbp)"],
    "balance": [
        "balance", "running balance", "closing balance", "balance £",
        "balance (£)", "ledger balance", "bal", "account balance",
    ],
}

# Which bank produced this PDF? Used only for the log and for per-bank quirks.
BANK_FINGERPRINTS: list[tuple[str, str]] = [
    ("Barclays", r"barclays"),
    ("HSBC", r"\bhsbc\b"),
    ("Lloyds", r"lloyds"),
    ("NatWest", r"natwest|national westminster"),
    ("Royal Bank of Scotland", r"royal bank of scotland|\brbs\b"),
    ("Santander", r"santander"),
    ("Halifax", r"halifax"),
    ("Nationwide", r"nationwide"),
    ("TSB", r"\btsb\b"),
    ("Metro Bank", r"metro bank"),
    ("Co-operative Bank", r"co-?operative bank|\bco-?op bank\b"),
    ("Virgin Money", r"virgin money"),
    ("Monzo", r"monzo"),
    ("Starling", r"starling"),
    ("Revolut", r"revolut"),
    ("Tide", r"\btide\b"),
    ("Wise", r"\bwise\b|transferwise"),
    ("Cashplus", r"cashplus"),
    ("Allied Irish (GB)", r"allied irish|\baib\b"),
    ("Bank of Scotland", r"bank of scotland"),
]


def detect_bank(text: str) -> str | None:
    head = text[:4000].lower()
    for name, pattern in BANK_FINGERPRINTS:
        if re.search(pattern, head):
            return name
    return None


# Statements often print dates without a year ("3 Apr"). The year lives in the
# statement period line at the top of page 1.
PERIOD_YEAR_RE = re.compile(
    r"(?:statement|period|from|between|for the period)[^\n]{0,80}?(20\d{2})", re.I)


def detect_statement_year(text: str) -> int | None:
    m = PERIOD_YEAR_RE.search(text[:3000])
    if m:
        return int(m.group(1))
    years = re.findall(r"\b(20[1-4]\d)\b", text[:1500])
    return int(years[0]) if years else None


HEADING_NOISE_RE = re.compile(r"\((?:£|\$|€|gbp|usd|eur|aed|pkr)\)|[£$€₹]|[():|.,*]", re.I)


def normalise_heading(text: str) -> str:
    """'Paid In(£)' -> 'paid in', 'Withdrawn(£)' -> 'withdrawn', 'Balance(£):' -> 'balance'."""
    out = HEADING_NOISE_RE.sub(" ", text.lower())
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class Column:
    name: str
    x0: float
    x1: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class PageLayout:
    columns: list[Column] = field(default_factory=list)
    header_bottom: float = 0.0

    @property
    def names(self) -> set[str]:
        return {c.name for c in self.columns}

    def is_usable(self) -> bool:
        n = self.names
        has_money = bool(n & {"debit", "credit", "amount", "balance"})
        return "date" in n and has_money


def group_words_into_lines(words: list[dict], tol: float = 2.5) -> list[list[dict]]:
    """Group pdfplumber words into visual lines by their 'top' coordinate."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def detect_layout(lines: list[list[dict]]) -> PageLayout:
    """Find the header row and turn it into x-ranges per column."""
    best = PageLayout()
    for line in lines:  # the header can sit well down the first page
        found: list[Column] = []
        used_x: list[float] = []
        i = 0
        while i < len(line):
            # longest heading phrase wins, so "Payment type and details" is read as
            # one description heading rather than a stray "type" column
            for span in (4, 3, 2, 1):
                chunk = line[i:i + span]
                if len(chunk) < span:
                    continue
                phrase = normalise_heading(" ".join(w["text"] for w in chunk))
                for col_name, aliases in COLUMN_ALIASES.items():
                    if phrase in aliases and col_name not in {c.name for c in found}:
                        found.append(Column(col_name, chunk[0]["x0"], chunk[-1]["x1"]))
                        used_x.append(chunk[0]["x0"])
                        i += span
                        break
                else:
                    continue
                break
            else:
                i += 1
        layout = PageLayout(found, max(w["bottom"] for w in line) if line else 0)
        if len(layout.columns) > len(best.columns) and layout.is_usable():
            best = layout
    if best.is_usable():
        best.columns.sort(key=lambda c: c.x0)
        _widen(best)
        return best
    return PageLayout()


NUMERIC_COLS = {"debit", "credit", "amount", "balance"}


def _widen(layout: PageLayout) -> None:
    """Turn heading positions into gap-free catchment bands.

    Numeric columns are right-aligned, so their values sit just left of the
    heading's right edge - they only need a narrow band. Text columns take all
    the remaining space, which matters because long descriptions overflow well
    past their own heading.
    """
    cols = layout.columns
    orig = [(c.x0, c.x1) for c in cols]

    starts: list[float] = []
    for i, c in enumerate(cols):
        prev_right = orig[i - 1][1] if i > 0 else 0.0
        if c.name in NUMERIC_COLS:
            starts.append(max(prev_right + 1.0, orig[i][1] - 78.0))
        else:
            starts.append(max(0.0, orig[i][0] - 4.0) if i > 0 else 0.0)

    for i, c in enumerate(cols):
        c.x0 = starts[i]
        if i + 1 < len(cols):
            c.x1 = starts[i + 1]
        else:
            # The right-hand column must stop at the edge of the table. Barclays and
            # others print an "At a glance" summary panel beside the transactions,
            # and an unbounded last column would read that panel as balances.
            c.x1 = orig[i][1] + 12.0


def row_separators(page, layout: PageLayout) -> list[float]:
    """Many statements rule a horizontal line between transactions. Those rules are
    a far more reliable row boundary than text positions, because one transaction
    can span several text lines with the money printed on the last of them."""
    if not layout.columns:
        return []
    right = max((c.x1 for c in layout.columns if c.x1 < 9000), default=0.0)
    span = max(right - min(c.x0 for c in layout.columns), 200.0)
    ys: list[float] = []
    for ln in page.lines:
        if abs(ln["y0"] - ln["y1"]) > 0.8:               # not horizontal
            continue
        if (ln["x1"] - ln["x0"]) < span * 0.55:           # not a full-width rule
            continue
        ys.append(round(ln["top"], 1))
    for r in page.rects:                                  # some banks use thin bars
        if r["bottom"] - r["top"] < 1.5 and (r["x1"] - r["x0"]) >= span * 0.55:
            ys.append(round(r["top"], 1))
    merged: list[float] = []
    for y in sorted(set(ys)):
        if not merged or y - merged[-1] > 2.0:
            merged.append(y)
    return merged


def clean_numeric_cell(cell: str) -> tuple[float | None, str]:
    """A numeric cell may have caught overflow text from the column to its left.
    Return (value, leftover_text_to_give_back_to_the_description)."""
    if not cell:
        return None, ""
    tokens = cell.split()
    money_idx = [i for i, t in enumerate(tokens) if looks_like_amount(t, strict=True)]
    if not money_idx:
        return None, cell
    i = money_idx[-1]
    return to_number(tokens[i]), " ".join(tokens[:i] + tokens[i + 1:]).strip()


def bucket(line: list[dict], layout: PageLayout) -> dict[str, str]:
    out = {c.name: [] for c in layout.columns}
    for w in line:
        cx = (w["x0"] + w["x1"]) / 2
        for c in layout.columns:
            if c.x0 <= cx < c.x1:
                out[c.name].append(w["text"])
                break
    return {k: " ".join(v).strip() for k, v in out.items()}


# --------------------------------------------------------------------------
# 4. Noise filtering
# --------------------------------------------------------------------------

NOISE_RE = re.compile(
    r"^(page \d+|continued|statement of account|opening balance|closing balance|"
    r"(balance )?brought forward|(balance )?carried forward|b/?f|c/?f|total|subtotal|"
    r"end of statement|tel[:.]|registered (office|in)|sort code|"
    r"account (no|number)|account opened)",
    re.I,
)


URL_LINE_RE = re.compile(r"^(https?://\S+|www\.\S+)$", re.I)


def is_noise(text: str) -> bool:
    """A footer or a heading, rather than part of a transaction. A bare URL on its
    own line is footer; a URL inside a description ("Card Payment to Www.Copart.Co.UK
    On 13 May") is the merchant's name and must be kept."""
    t = text.strip()
    if not t:
        return True
    return bool(NOISE_RE.match(t)) or bool(URL_LINE_RE.match(t))


# --------------------------------------------------------------------------
# 5. The parser
# --------------------------------------------------------------------------

@dataclass
class Options:
    dayfirst: bool = True          # True = UK/PK (31/01/2026). False = US (01/31/2026).
    default_year: int | None = None   # None = read it off the statement itself
    invert: bool = False           # flip debit/credit if your bank is unusual
    ocr: bool = True               # read pages that have no text layer
    ocr_dpi: int = 300
    min_columns_confidence: int = 3


# --------------------------------------------------------------------------
# 5a. OCR fallback - for scans, and for PDFs whose text was flattened to shapes
# --------------------------------------------------------------------------

OCR_UNAVAILABLE = (
    "This PDF has no text in it, and OCR is not installed. Install it with:\n"
    "    pip install pytesseract pypdfium2 pillow\n"
    "    Windows: also install Tesseract from github.com/UB-Mannheim/tesseract/wiki\n"
    "    Mac: brew install tesseract      Linux: sudo apt install tesseract-ocr"
)


def ocr_page_words(raw: bytes, page_index: int, dpi: int = 300) -> list[dict]:
    """Render one page and read it with OCR, returning words in the same shape
    pdfplumber uses, so the rest of the parser cannot tell the difference."""
    import pypdfium2 as pdfium
    import pytesseract
    from pytesseract import Output

    page = pdfium.PdfDocument(raw)[page_index]
    image = page.render(scale=dpi / 72).to_pil()
    data = pytesseract.image_to_data(image, output_type=Output.DICT, config="--psm 6")

    scale = 72 / dpi
    words: list[dict] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 35:
            continue
        x, y = data["left"][i] * scale, data["top"][i] * scale
        w, h = data["width"][i] * scale, data["height"][i] * scale
        words.append({"text": text, "x0": x, "x1": x + w,
                      "top": y, "bottom": y + h, "upright": True})
    return words


class StatementParser:
    def __init__(self, options: Options | None = None):
        self.opt = options or Options()
        self.log: list[str] = []
        self.bank: str | None = None
        self.used_ocr = False

    @staticmethod
    def _raw_bytes(file) -> bytes | None:
        """Keep the original bytes around so a page can be re-rendered for OCR."""
        try:
            if isinstance(file, (str, Path)):
                return Path(file).read_bytes()
            pos = file.tell()
            data = file.read()
            file.seek(pos)
            return data
        except Exception:                                # noqa: BLE001
            return None

    # -- public ------------------------------------------------------------
    def parse(self, file, password: str | None = None, source: str = "statement.pdf") -> pd.DataFrame:
        raw = self._raw_bytes(file)
        pages: list[tuple[int, list, object]] = []

        with pdfplumber.open(io.BytesIO(raw) if raw else file, password=password or "") as pdf:
            first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
            for pno, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                if not words and raw and self.opt.ocr:
                    try:
                        words = ocr_page_words(raw, pno - 1, self.opt.ocr_dpi)
                        self.log.append(f"Page {pno}: no text layer - read with OCR "
                                        f"({len(words)} words)")
                        self.used_ocr = True
                        if not first_text.strip():
                            first_text = " ".join(w["text"] for w in words)
                    except ImportError:
                        self.log.append(f"Page {pno}: {OCR_UNAVAILABLE}")
                    except Exception as exc:                     # noqa: BLE001
                        self.log.append(f"Page {pno}: OCR failed - {exc}")
                if not words:
                    self.log.append(f"Page {pno}: no readable text - skipped")
                    continue
                pages.append((pno, group_words_into_lines(words), page))

        self.bank = detect_bank(first_text)
        if self.bank:
            self.log.append(f"Bank detected: {self.bank}")
        if self.opt.default_year is None:
            year = detect_statement_year(first_text)
            if year:
                self.opt.default_year = year
                self.log.append(f"Statement year: {year} (for dates without one)")

        if not pages:
            return self._empty()

        # Work out the column layout once - most banks only print the headings on
        # the first page, and every later page uses the same geometry.
        layouts: dict[int, PageLayout] = {}
        carried: PageLayout | None = None
        for pno, lines, _ in pages:
            found = detect_layout(lines)
            if found.is_usable() and len(found.columns) >= self.opt.min_columns_confidence:
                carried = found
                layouts[pno] = found
            elif carried is not None:
                cont = PageLayout(list(carried.columns), 0.0)   # no heading to skip
                layouts[pno] = cont
        if not layouts:
            return self._fallback_document(pages)

        best_name, best_df, best_score = None, None, (-1.0, -1.0, -1)
        for name in ("gaps", "ruled", "lines"):
            rows: list[dict] = []
            self._last_date = None
            for pno, lines, page in pages:
                layout = layouts.get(pno)
                if layout is None:
                    continue
                if name == "ruled":
                    seps = row_separators(page, layout)
                    if len(seps) < 2:
                        continue
                    got = self._parse_ruled(lines, layout, seps, pno)
                    if not getattr(self, "_ruled_ok", True):
                        got = []
                elif name == "gaps":
                    got = self._parse_gaps(lines, layout, pno)
                else:
                    got = self._parse_columns(lines, layout, pno)
                rows.extend(got)
            if not rows:
                continue
            df = self._finalise(pd.DataFrame(rows))
            score = self._score(df)
            self.log.append(f"{name} mode: {len(df)} rows, "
                            f"{score[0] * 100:.0f}% tie to the running balance")
            if score > best_score:
                best_name, best_df, best_score = name, df, score

        if best_df is None or best_df.empty:
            return self._fallback_document(pages)

        self.log.append(f"Using {best_name} mode - {len(best_df)} transactions")
        best_df["Source File"] = source
        return best_df

    def _fallback_document(self, pages) -> pd.DataFrame:
        """No heading row anywhere: date-anchored lines, or labelled blocks."""
        rows: list[dict] = []
        self._last_date = None
        for pno, lines, _ in pages:
            if self.looks_like_blocks(lines):
                rows.extend(self._parse_blocks(lines, pno))
            else:
                rows.extend(self._parse_fallback(lines, pno))
        if not rows:
            return self._empty()
        self.log.append(f"No heading row found - read {len(rows)} rows by date")
        return self._finalise(pd.DataFrame(rows))

    @staticmethod
    def _score(df: pd.DataFrame) -> tuple[float, float, int]:
        """How believable is this reading? The running balance is the referee.

        Returned as (how many rows tie, how many rows are complete, how many rows).
        Completeness matters: a reading that ties perfectly but drags in stray
        figures from an appendix should lose to a clean one.
        """
        if df.empty or "Balance" not in df:
            return (0.0, 0.0, 0)
        complete = df["Balance"].notna() & df["Amount"].notna()
        coverage = float(complete.mean())
        valid = df[complete]
        if len(valid) < 2:
            return (0.0, coverage, len(df))
        good = total = 0
        prev = None
        for _, r in valid.iterrows():
            if prev is not None:
                total += 1
                if abs(prev + r["Amount"] - r["Balance"]) <= 0.011:
                    good += 1
            prev = float(r["Balance"])
        return (good / total if total else 0.0, coverage, len(df))

    # -- mode 1: column bucketing ------------------------------------------
    def _parse_columns(self, lines, layout: PageLayout, pno: int) -> list[dict]:
        rows: list[dict] = []
        for line in lines:
            if line[0]["top"] <= layout.header_bottom:
                continue
            cells = bucket(line, layout)
            date_cell = cells.get("date", "")
            desc_cell = cells.get("description", "")
            dt, _ = parse_date(date_cell, self.opt.dayfirst, self.opt.default_year)

            if dt is None:
                # continuation line for the previous transaction
                if rows and desc_cell and not is_noise(desc_cell) and not date_cell:
                    rows[-1]["Description"] = (rows[-1]["Description"] + " " + desc_cell).strip()
                continue
            if is_noise(desc_cell) and not any(
                cells.get(k) for k in ("debit", "credit", "amount")
            ):
                continue

            values: dict[str, float | None] = {}
            spill: list[str] = []
            for key in ("debit", "credit", "amount", "balance"):
                val, leftover = clean_numeric_cell(cells.get(key, ""))
                values[key] = val
                if leftover:
                    spill.append(leftover)

            row = {
                "Date": dt,
                "Description": (desc_cell + " " + " ".join(spill)).strip(),
                "Code": cells.get("code", ""),
                "Debit": values["debit"],
                "Credit": values["credit"],
                "Amount": values["amount"],
                "Balance": values["balance"],
                "Page": pno,
            }
            if row["Debit"] is not None:
                row["Debit"] = abs(row["Debit"])
            if row["Credit"] is not None:
                row["Credit"] = abs(row["Credit"])
            if all(row[k] is None for k in ("Debit", "Credit", "Amount", "Balance")):
                continue
            rows.append(row)
        return rows

    # -- mode 2: date-anchored lines ---------------------------------------
    def _parse_fallback(self, lines, pno: int) -> list[dict]:
        rows: list[dict] = []
        for line in lines:
            text = " ".join(w["text"] for w in line).strip()
            dt, raw = parse_date(text, self.opt.dayfirst, self.opt.default_year)
            if dt is None:
                if rows and text and not is_noise(text) and not looks_like_amount(text.split()[-1], strict=True):
                    rows[-1]["Description"] = (rows[-1]["Description"] + " " + text).strip()
                continue
            rest = text[len(raw):].strip()
            desc, nums = trailing_amounts(rest)
            if not nums:
                continue
            row = {"Date": dt, "Description": desc.strip(" -|"), "Debit": None,
                   "Credit": None, "Amount": None, "Balance": None, "Page": pno}
            if len(nums) == 1:
                row["Amount"] = nums[0]
            elif len(nums) == 2:
                row["Amount"], row["Balance"] = nums[0], nums[1]
            else:  # 3+ -> debit, credit, balance
                row["Debit"], row["Credit"], row["Balance"] = abs(nums[-3]), abs(nums[-2]), nums[-1]
            rows.append(row)
        return self._signs_from_balance(rows)

    MONEY_IN_WORDS = re.compile(
        r"\b(credit|deposit|received|receipt|refund|reversal|salary|payroll|"
        r"interest|transfer in|inward|cash in|bacs credit|incoming)\b", re.I)
    MONEY_OUT_WORDS = re.compile(
        r"\b(debit|withdraw|payment|paid|purchase|card|pos|atm|fee|charge|"
        r"standing order|direct debit|transfer out|outward|cash out|outgoing|bill)\b", re.I)

    # -- mode 1b: ruled rows (one transaction per boxed row) ---------------
    def _parse_ruled(self, lines, layout: PageLayout, seps: list[float], pno: int) -> list[dict]:
        """Group text lines into the bands marked out by the horizontal rules, so a
        transaction spanning several printed lines still becomes one row."""
        bands: dict[int, list] = {}
        for line in lines:
            top = line[0]["top"]
            if top <= layout.header_bottom:
                continue
            idx = None
            for i in range(len(seps) - 1):
                if seps[i] - 1.0 <= top < seps[i + 1] - 1.0:
                    idx = i
                    break
            if idx is None and seps and top >= seps[-1] - 1.0:
                idx = len(seps) - 1
            if idx is not None:
                bands.setdefault(idx, []).append(line)

        rows: list[dict] = []
        self._ruled_ok = True
        money_col = next((c.name for c in layout.columns if c.name == "balance"), None)
        for idx in sorted(bands):
            words = sorted((w for line in bands[idx] for w in line),
                           key=lambda w: (round(w["top"], 1), w["x0"]))
            if money_col:
                # a genuine row band holds at most one balance figure; more than one
                # means these rules are page furniture, not row separators
                n = sum(1 for w in words
                        if looks_like_amount(w["text"], strict=True)
                        and any(c.name == money_col and c.x0 <= (w["x0"] + w["x1"]) / 2 < c.x1
                                for c in layout.columns))
                if n > 1:
                    self._ruled_ok = False
            row = self._row_from_words(words, layout, pno)
            if row:
                rows.append(row)
        return rows

    def _resolve_year(self, dt, raw):
        """Statements print '06 AUG' with no year. Anchor those to the last date we
        saw that did carry one, rolling the year over at each December -> January."""
        if dt is None:
            return None
        prev = getattr(self, "_last_date", None)
        if date_has_year(raw or ""):
            return dt
        if prev is None:
            return dt
        candidate = dt.replace(year=prev.year)
        if (prev - candidate).days > 60:          # wrapped into the new year
            candidate = candidate.replace(year=prev.year + 1)
        elif (candidate - prev).days > 300:       # stale anchor, pull it back
            candidate = candidate.replace(year=prev.year - 1)
        return candidate

    @staticmethod
    def reading_order(words: list[dict]) -> list[dict]:
        """Put a band's words back into human reading order. Sorting on the raw y
        coordinate is not enough: banks often centre the date and amount between two
        lines of description, and OCR jitters baselines by a point or two."""
        if not words:
            return []
        heights = sorted(w["bottom"] - w["top"] for w in words)
        tol = max(2.0, 0.6 * heights[len(heights) // 2])
        ordered = sorted(words, key=lambda w: w["top"])
        rows: list[list[dict]] = [[ordered[0]]]
        for w in ordered[1:]:
            if w["top"] - rows[-1][0]["top"] <= tol:
                rows[-1].append(w)
            else:
                rows.append([w])
        out: list[dict] = []
        for r in rows:
            out.extend(sorted(r, key=lambda w: w["x0"]))
        return out

    def _row_from_words(self, words, layout: PageLayout, pno: int) -> dict | None:
        """Assign each word to a column, pull one value out of each numeric column,
        and let every remaining word fall into the description in reading order."""
        words = self.reading_order(list(words))
        assigned: list[tuple[str, dict]] = []
        for w in words:
            cx = (w["x0"] + w["x1"]) / 2
            name = next((c.name for c in layout.columns if c.x0 <= cx < c.x1), None)
            if name:
                assigned.append((name, w))

        date_text = " ".join(w["text"] for n, w in assigned if n == "date")
        dt, raw = parse_date(date_text, self.opt.dayfirst, self.opt.default_year)
        dt = self._resolve_year(dt, raw)
        if dt is not None:
            self._last_date = dt
        dt = dt or getattr(self, "_last_date", None)

        values: dict[str, float | None] = {}
        consumed: set[int] = set()
        for col in ("debit", "credit", "amount", "balance"):
            hits = [(i, w) for i, (n, w) in enumerate(assigned)
                    if n == col and looks_like_amount(w["text"], strict=True)]
            if hits:
                i, w = hits[-1]
                values[col] = to_number(w["text"])
                consumed.add(i)
            else:
                values[col] = None

        desc = " ".join(
            w["text"] for i, (n, w) in enumerate(assigned)
            if i not in consumed and n != "date"
        ).strip()

        if dt is None or all(v is None for v in values.values()):
            return None
        if is_noise(desc):
            return None      # BROUGHT FORWARD, Account Opened, Total Payments, footers

        return {"Date": dt, "Description": desc,
                "Code": " ".join(w["text"] for n, w in assigned if n == "code").strip(),
                "Debit": abs(values["debit"]) if values["debit"] is not None else None,
                "Credit": abs(values["credit"]) if values["credit"] is not None else None,
                "Amount": values["amount"], "Balance": values["balance"], "Page": pno}

    def _row_from_cells(self, cells: dict[str, str], pno: int) -> dict | None:
        """Turn one row's cells into a transaction, carrying the date forward when
        the bank only prints it on the first row of each day."""
        dt, _ = parse_date(cells.get("date", ""), self.opt.dayfirst, self.opt.default_year)
        if dt is not None:
            self._last_date = dt
        dt = dt or getattr(self, "_last_date", None)

        values: dict[str, float | None] = {}
        spill: list[str] = []
        for key in ("debit", "credit", "amount", "balance"):
            val, leftover = clean_numeric_cell(cells.get(key, ""))
            values[key] = val
            if leftover:
                spill.append(leftover)

        desc = (cells.get("description", "") + " " + " ".join(spill)).strip()
        if dt is None:
            return None
        if all(v is None for v in values.values()):
            return None
        if is_noise(desc) and values["debit"] is None and values["credit"] is None \
                and values["amount"] is None:
            return None      # BROUGHT FORWARD / carried balance rows

        return {"Date": dt, "Description": desc,
                "Code": " ".join(w["text"] for n, w in assigned if n == "code").strip(),
                "Debit": abs(values["debit"]) if values["debit"] is not None else None,
                "Credit": abs(values["credit"]) if values["credit"] is not None else None,
                "Amount": values["amount"], "Balance": values["balance"], "Page": pno}

    # -- mode 1c: gap bands (no rules; transactions separated by whitespace) --
    @staticmethod
    def gap_bands(lines, header_bottom: float) -> list[list]:
        """Cluster printed lines into transactions using the vertical gaps between
        them. Within one transaction the lines are tightly spaced; between two
        transactions there is a visibly bigger gap. This copes with layouts that
        centre the date and amount between two lines of description."""
        body = [ln for ln in lines if ln[0]["top"] > header_bottom]
        if len(body) < 4:
            return [[ln] for ln in body]
        tops = [ln[0]["top"] for ln in body]
        gaps = sorted(b - a for a, b in zip(tops, tops[1:]) if b > a)
        if not gaps:
            return [[ln] for ln in body]
        tight = gaps[len(gaps) // 3]                      # a within-transaction gap
        threshold = max(tight * 1.8, tight + 3.0)

        bands: list[list] = [[body[0]]]
        for prev, ln in zip(body, body[1:]):
            if ln[0]["top"] - prev[0]["top"] > threshold:
                bands.append([ln])
            else:
                bands[-1].append(ln)
        return bands

    def _parse_gaps(self, lines, layout: PageLayout, pno: int) -> list[dict]:
        rows: list[dict] = []
        for band in self.gap_bands(lines, layout.header_bottom):
            words = sorted((w for ln in band for w in ln),
                           key=lambda w: (round(w["top"], 1), w["x0"]))
            row = self._row_from_words(words, layout, pno)
            if row:
                rows.append(row)
        return rows

    # -- mode 3: labelled blocks (Santander UK and similar) ----------------
    BLOCK_LABELS = {
        "date": "date",
        "description": "description",
        "details": "description",
        "amount": "amount",
        "balance": "balance",
        "money in": "credit",
        "money out": "debit",
        "paid in": "credit",
        "paid out": "debit",
    }

    @staticmethod
    def looks_like_blocks(lines) -> bool:
        """Santander prints each transaction as Date:/Description:/Amount:/Balance:."""
        labelled = 0
        for line in lines:
            text = " ".join(w["text"] for w in line).lower()
            if re.match(r"^(date|description|amount|balance|money in|money out)\s*:", text):
                labelled += 1
        return labelled >= 6

    def _parse_blocks(self, lines, pno: int) -> list[dict]:
        rows: list[dict] = []
        current: dict = {}

        def flush():
            if current.get("Date") is not None and (
                current.get("Amount") is not None
                or current.get("Debit") is not None
                or current.get("Credit") is not None
                or current.get("Balance") is not None
            ):
                rows.append({"Date": current.get("Date"),
                             "Description": current.get("Description", ""),
                             "Debit": current.get("Debit"), "Credit": current.get("Credit"),
                             "Amount": current.get("Amount"), "Balance": current.get("Balance"),
                             "Page": pno})
            current.clear()

        for line in lines:
            text = " ".join(w["text"] for w in line).strip()
            m = re.match(r"^([A-Za-z][A-Za-z ]{2,20}?)\s*:\s*(.*)$", text)
            if not m:
                if current.get("Description") and text and not is_noise(text):
                    current["Description"] = (current["Description"] + " " + text).strip()
                continue
            label, value = m.group(1).strip().lower(), m.group(2).strip()
            field_name = self.BLOCK_LABELS.get(label)
            if field_name is None:
                continue
            if field_name == "date":
                flush()  # a new Date: line starts a new transaction
                dt, _ = parse_date(value, self.opt.dayfirst, self.opt.default_year)
                if dt is None:
                    continue
                current["Date"] = dt
                current.setdefault("Description", "")
            elif field_name == "description":
                current["Description"] = value
            else:
                val = to_number(value)
                if val is None:
                    continue
                key = {"amount": "Amount", "debit": "Debit", "credit": "Credit",
                       "balance": "Balance"}[field_name]
                current[key] = abs(val) if key in ("Debit", "Credit") else val
        flush()
        return self._signs_from_balance(rows)

    def _signs_from_balance(self, rows: list[dict]) -> list[dict]:
        """If we only got (amount, balance), use the balance movement to set the sign."""
        prev_bal = None
        for r in rows:
            amt, bal = r.get("Amount"), r.get("Balance")
            resolved = False
            if amt is not None and bal is not None and prev_bal is not None:
                if abs((prev_bal - abs(amt)) - bal) < 0.011:
                    r["Amount"] = -abs(amt)
                    resolved = True
                elif abs((prev_bal + abs(amt)) - bal) < 0.011:
                    r["Amount"] = abs(amt)
                    resolved = True
            if not resolved and amt is not None:
                r["_sign_unknown"] = True
            if bal is not None:
                prev_bal = bal

        # rows with no previous balance to compare against: use the wording,
        # then sanity-check against the next row's balance if we have one.
        for i, r in enumerate(rows):
            if not r.pop("_sign_unknown", False) or r.get("Amount") is None:
                continue
            amt, bal = abs(r["Amount"]), r.get("Balance")
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if bal is not None and nxt and nxt.get("Balance") is not None and nxt.get("Amount") is not None:
                pass  # nothing extra to infer; wording decides
            if self.MONEY_IN_WORDS.search(r["Description"]) and not \
                    self.MONEY_OUT_WORDS.search(r["Description"]):
                r["Amount"] = amt
            elif self.MONEY_OUT_WORDS.search(r["Description"]):
                r["Amount"] = -amt
        return rows

    @staticmethod
    def _fix_year_rollover(rows: list[dict]) -> list[dict]:
        """A statement spanning Dec->Jan with year-less dates needs the year bumping."""
        prev = None
        bump = 0
        for r in rows:
            dt = r.get("Date")
            if dt is None:
                continue
            if prev is not None and dt.month < prev.month - 6:
                bump += 1
            prev = dt
            if bump:
                try:
                    r["Date"] = dt.replace(year=dt.year + bump)
                except ValueError:      # 29 Feb edge case
                    r["Date"] = dt.replace(year=dt.year + bump, day=28)
        return rows

    # -- shared finishing ---------------------------------------------------
    def _finalise(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in ("Debit", "Credit", "Amount", "Balance"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # derive Amount from Debit/Credit, or Debit/Credit from Amount
        def amount(r):
            if pd.notna(r.get("Amount")):
                return float(r["Amount"])
            d = r.get("Debit")
            c = r.get("Credit")
            d = float(d) if pd.notna(d) else 0.0
            c = float(c) if pd.notna(c) else 0.0
            return (c - d) if (d or c) else None

        df["Amount"] = df.apply(amount, axis=1)
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
        if self.opt.invert:
            df["Amount"] = -df["Amount"]
        df["Debit"] = df["Amount"].apply(lambda v: abs(v) if pd.notna(v) and v < 0 else None)
        df["Credit"] = df["Amount"].apply(lambda v: v if pd.notna(v) and v > 0 else None)
        df["Description"] = (
            df["Description"].fillna("").str.replace(r"\s+", " ", regex=True).str.strip(" -|")
        )
        df["Type"] = df["Amount"].apply(
            lambda v: "Credit" if pd.notna(v) and v > 0 else ("Debit" if pd.notna(v) else "")
        )
        df = df.dropna(subset=["Date"])
        both = df["Amount"].notna() & df["Balance"].notna()
        if len(df) >= 4 and both.mean() >= 0.7:
            dropped = int((~both).sum())
            if dropped:
                self.log.append(f"Ignored {dropped} row(s) with no running balance "
                                f"- not part of the transaction table")
            df = df[both]
        df = self._to_chronological(df)
        df = df[~((df["Amount"].isna()) & (df["Balance"].isna()))]
        if "Code" in df and not df["Code"].astype(str).str.strip().any():
            df = df.drop(columns=["Code"])
        if "Source File" not in df:
            df["Source File"] = ""
        cols = ["Date", "Description", "Code", "Debit", "Credit", "Amount",
                "Balance", "Type", "Page", "Source File"]
        return df[[c for c in cols if c in df.columns]].reset_index(drop=True)

    def _to_chronological(self, df: pd.DataFrame) -> pd.DataFrame:
        """Some banks (TSB, most challenger apps) print the newest transaction first.
        The running balance only makes sense oldest-first, so flip the whole table -
        reversing rather than sorting keeps same-day transactions in the right order."""
        if len(df) < 3:
            return df
        d = df["Date"].tolist()
        down = sum(1 for a, b in zip(d, d[1:]) if b < a)
        up = sum(1 for a, b in zip(d, d[1:]) if b > a)
        if down > up:
            self.log.append("Statement is printed newest-first - order reversed")
            df = df.iloc[::-1].reset_index(drop=True)
        return df

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(columns=["Date", "Description", "Code", "Debit", "Credit",
                                     "Amount", "Balance", "Type", "Page", "Source File"])


# --------------------------------------------------------------------------
# 6. Sanity check - the bit accountants actually care about
# --------------------------------------------------------------------------

def reconcile(df: pd.DataFrame, tolerance: float = 0.02) -> dict:
    """Verify that balance(n-1) + amount(n) == balance(n) for every row."""
    out = {"checked": 0, "mismatches": [], "opening": None, "closing": None,
           "total_in": 0.0, "total_out": 0.0, "ok": True, "message": ""}
    if df.empty:
        out["ok"] = False
        out["message"] = "No transactions were extracted."
        return out

    out["total_in"] = float(df["Credit"].fillna(0).sum())
    out["total_out"] = float(df["Debit"].fillna(0).sum())

    bal = df["Balance"]
    if bal.isna().all():
        out["message"] = "No balance column found - running-balance check skipped."
        return out

    valid = df[bal.notna() & df["Amount"].notna()]
    if len(valid) < 2:
        out["message"] = "Not enough balance rows to reconcile."
        return out

    out["opening"] = float(valid.iloc[0]["Balance"] - valid.iloc[0]["Amount"])
    out["closing"] = float(valid.iloc[-1]["Balance"])

    prev = None
    for idx, r in valid.iterrows():
        if prev is not None:
            expected = prev + r["Amount"]
            if abs(expected - r["Balance"]) > tolerance:
                out["mismatches"].append(
                    {"row": int(idx) + 2, "date": r["Date"], "description": r["Description"][:40],
                     "expected": round(expected, 2), "found": round(float(r["Balance"]), 2),
                     "diff": round(float(r["Balance"]) - expected, 2)}
                )
            out["checked"] += 1
        prev = float(r["Balance"])

    out["ok"] = not out["mismatches"]
    out["message"] = (
        f"All {out['checked']} rows reconcile against the running balance."
        if out["ok"] else
        f"{len(out['mismatches'])} of {out['checked']} rows do not match the running balance."
    )
    return out


# --------------------------------------------------------------------------
# 7. Export
# --------------------------------------------------------------------------

def to_excel_bytes(df: pd.DataFrame, check: dict | None = None) -> bytes:
    buf = io.BytesIO()
    out = df.copy()
    if "Date" in out:
        out["Date"] = pd.to_datetime(out["Date"]).dt.date
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        out.to_excel(xl, sheet_name="Transactions", index=False)
        ws = xl.sheets["Transactions"]
        widths = {"Date": 12, "Description": 52, "Debit": 14, "Credit": 14,
                  "Amount": 14, "Balance": 16, "Type": 9, "Page": 6, "Source File": 26}
        for i, col in enumerate(out.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(col, 14)
            ws.cell(row=1, column=i).font = ws.cell(row=1, column=i).font.copy(bold=True)
            if col in {"Debit", "Credit", "Amount", "Balance"}:
                for r in range(2, len(out) + 2):
                    ws.cell(row=r, column=i).number_format = "#,##0.00"
        ws.freeze_panes = "A2"

        if check:
            summary = pd.DataFrame(
                [("Transactions", len(out)),
                 ("Opening balance", check.get("opening")),
                 ("Closing balance", check.get("closing")),
                 ("Total money in", round(check.get("total_in", 0), 2)),
                 ("Total money out", round(check.get("total_out", 0), 2)),
                 ("Balance check", check.get("message"))],
                columns=["Item", "Value"],
            )
            summary.to_excel(xl, sheet_name="Summary", index=False)
            xl.sheets["Summary"].column_dimensions["A"].width = 22
            xl.sheets["Summary"].column_dimensions["B"].width = 60
            if check.get("mismatches"):
                pd.DataFrame(check["mismatches"]).to_excel(xl, sheet_name="Check Failures", index=False)
    return buf.getvalue()


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    out = df.copy()
    if "Date" in out:
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
    return out.to_csv(index=False).encode("utf-8")


# --------------------------------------------------------------------------
# 8. Accounting-software export flavours
# --------------------------------------------------------------------------

def to_accounting_csv(df: pd.DataFrame, flavour: str = "standard") -> bytes:
    """Reshape into the layout each package expects on import."""
    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"])
    f = flavour.lower()

    if f == "quickbooks":                      # 3-column CSV
        out = pd.DataFrame({
            "Date": d["Date"].dt.strftime("%d/%m/%Y"),
            "Description": d["Description"],
            "Amount": d["Amount"].round(2),
        })
    elif f == "xero":
        out = pd.DataFrame({
            "*Date": d["Date"].dt.strftime("%d/%m/%Y"),
            "*Amount": d["Amount"].round(2),
            "Payee": d["Description"].str.slice(0, 50),
            "Description": d["Description"],
            "Reference": "",
        })
    elif f == "sage":
        out = pd.DataFrame({
            "Date": d["Date"].dt.strftime("%d/%m/%Y"),
            "Reference": "",
            "Details": d["Description"],
            "Debit": d["Debit"].round(2),
            "Credit": d["Credit"].round(2),
        })
    else:
        return to_csv_bytes(df)
    return out.to_csv(index=False).encode("utf-8")


EXPORT_FLAVOURS = ["standard", "quickbooks", "xero", "sage"]
