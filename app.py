"""
app.py — drag-and-drop web UI.

Run it:   streamlit run app.py
Opens at: http://localhost:8501

Each uploaded PDF keeps its own tab, its own balance check and its own download
buttons. Nothing is merged unless you ask for it.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

from statement_parser import (
    EXPORT_FLAVOURS,
    Options,
    StatementParser,
    reconcile,
    to_accounting_csv,
    to_excel_bytes,
)

st.set_page_config(page_title="Bank Statement Converter", page_icon="📄", layout="wide")

BLUE, GREEN = "#2D99D0", "#66B937"

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 2rem; max-width: 1500px; }}
      h1 {{ color: {BLUE}; font-size: 1.9rem !important; }}
      .stDownloadButton button {{ background: {GREEN}; color: white; border: 0; }}
      .stDownloadButton button:hover {{ background: #58a52f; color: white; }}
      div[data-testid="stMetricValue"] {{ font-size: 1.3rem; }}
      button[data-baseweb="tab"] {{ font-size: 0.9rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Bank Statement Converter")
st.caption("PDF statements → Excel / CSV. Runs entirely on this machine — nothing is uploaded anywhere.")


def safe_name(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "statement"


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    date_style = st.radio(
        "Date format in the PDF",
        ["Day first — 31/01/2026 (UK, PK, UAE)", "Month first — 01/31/2026 (US)"],
    )
    dayfirst = date_style.startswith("Day")

    password = st.text_input("PDF password (if locked)", type="password",
                             help="Many banks lock statements with your CNIC, DOB or account number.")

    auto_year = st.checkbox(
        "Read the year off the statement", value=True,
        help="Most statements print dates like '06 AUG' with the year only in the "
             "period line at the top. Leave this ticked unless that goes wrong.")
    default_year = None
    if not auto_year:
        default_year = st.number_input("Year to assume", min_value=2000, max_value=2100,
                                       value=datetime.now().year, step=1)

    invert = st.checkbox("Flip debit / credit",
                         help="Tick this only if money-in and money-out come out the wrong way round.")
    use_ocr = st.checkbox(
        "Read image-only PDFs (OCR)", value=True,
        help="Needed for scans, and for PDFs whose text was flattened to shapes. "
             "Slower, and figures should be spot-checked.")

    flavour = st.selectbox("CSV layout", EXPORT_FLAVOURS, format_func=lambda s: s.title())
    st.divider()
    st.caption("Wrong output? Send the PDF to whoever maintains this — most layouts "
               "are a small change to statement_parser.py.")

# ---------------------------------------------------------------- upload
files = st.file_uploader("Drop your PDF statements here", type=["pdf"], accept_multiple_files=True)

if not files:
    st.info("Upload one or more PDF statements. Each file gets its own tab, its own "
            "balance check and its own download buttons.")
    st.stop()

opts = Options(dayfirst=dayfirst,
               default_year=int(default_year) if default_year else None,
               invert=invert, ocr=use_ocr)

# ---------------------------------------------------------------- parse
results: list[dict] = []
progress = st.progress(0.0, text="Reading…")
for i, f in enumerate(files, start=1):
    progress.progress(i / len(files), text=f"Reading {f.name}…")
    parser = StatementParser(opts)
    entry = {"name": f.name, "df": None, "error": None,
             "log": [], "bank": None, "ocr": False}
    try:
        df = parser.parse(io.BytesIO(f.getvalue()), password=password or None, source=f.name)
        entry["log"] = parser.log
        entry["bank"] = parser.bank
        entry["ocr"] = parser.used_ocr
        if df.empty:
            entry["error"] = ("No transactions found. If this PDF is a scan, tick "
                              "\u201cRead image-only PDFs (OCR)\u201d in the sidebar.")
        else:
            entry["df"] = df
    except Exception as exc:                                       # noqa: BLE001
        msg = str(exc)
        if "password" in msg.lower() or "decrypt" in msg.lower():
            msg = "This PDF is password protected — enter the password in the sidebar."
        entry["error"] = msg
    results.append(entry)
progress.empty()

good = [r for r in results if r["df"] is not None]
bad = [r for r in results if r["df"] is None]

for r in bad:
    st.error(f"**{r['name']}** — {r['error']}")

if not good:
    st.stop()

for r in good:
    r["check"] = reconcile(r["df"])

# ---------------------------------------------------------------- overview
clean = sum(1 for r in good if r["check"]["ok"])
total_txns = sum(len(r["df"]) for r in good)

c1, c2, c3 = st.columns(3)
c1.metric("Files converted", f"{len(good)} of {len(results)}")
c2.metric("Transactions", f"{total_txns:,}")
c3.metric("Files passing the balance check", f"{clean} of {len(good)}")

stamp = datetime.now().strftime("%Y%m%d-%H%M")

# ---------------------------------------------------------------- per file
st.divider()
tabs = st.tabs([r["name"] for r in good])

for tab, r in zip(tabs, good):
    with tab:
        df, check = r["df"], r["check"]
        stem = safe_name(r["name"])

        head = st.columns(4)
        head[0].metric("Transactions", len(df))
        head[1].metric("Money in", f"{df['Credit'].fillna(0).sum():,.2f}")
        head[2].metric("Money out", f"{df['Debit'].fillna(0).sum():,.2f}")
        head[3].metric("Net", f"{df['Amount'].fillna(0).sum():,.2f}")

        bits = []
        if r["bank"]:
            bits.append(f"Bank: **{r['bank']}**")
        if check.get("opening") is not None:
            bits.append(f"Opening **{check['opening']:,.2f}** → closing **{check['closing']:,.2f}**")
        if bits:
            st.caption("  ·  ".join(bits))

        if r["ocr"]:
            st.info("This PDF had no text in it, so it was read with OCR. The balance "
                    "check below is your assurance the figures came through correctly.")

        (st.success if check["ok"] else st.warning)(check["message"])
        if check["mismatches"]:
            with st.expander(f"Rows that don't tie to the running balance "
                             f"({len(check['mismatches'])})"):
                st.dataframe(pd.DataFrame(check["mismatches"]), use_container_width=True)
                st.caption("Usually a wrapped description or a fee line read wrongly. "
                           "Fix it in the table below — the downloads use your edits.")

        edited = st.data_editor(
            df, use_container_width=True, hide_index=True, num_rows="dynamic",
            key=f"editor-{stem}-{len(df)}",
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                "Debit": st.column_config.NumberColumn("Debit", format="%.2f"),
                "Credit": st.column_config.NumberColumn("Credit", format="%.2f"),
                "Amount": st.column_config.NumberColumn("Amount", format="%.2f"),
                "Balance": st.column_config.NumberColumn("Balance", format="%.2f"),
                "Code": st.column_config.TextColumn("Code", width="small"),
            },
            height=430,
        )
        r["edited"] = edited

        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button(
            "Download Excel", to_excel_bytes(edited, reconcile(edited)),
            f"{stem}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, key=f"xlsx-{stem}")
        d2.download_button(
            f"Download CSV ({flavour})", to_accounting_csv(edited, flavour),
            f"{stem}.csv", "text/csv",
            use_container_width=True, key=f"csv-{stem}")

        with st.expander("How this PDF was read"):
            for line in r["log"]:
                st.text(line)

# ---------------------------------------------------------------- download all
st.divider()
st.subheader("Download everything")

frames = {safe_name(r["name"]): r.get("edited", r["df"]) for r in good}


def zip_of(kind: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for stem, d in frames.items():
            if kind == "csv":
                z.writestr(f"{stem}.csv", to_accounting_csv(d, flavour))
            else:
                z.writestr(f"{stem}.xlsx", to_excel_bytes(d, reconcile(d)))
    return buf.getvalue()


def one_workbook() -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for stem, d in frames.items():
            sheet = stem[:28] or "Sheet"
            n = 2
            while sheet in used:
                sheet = f"{stem[:25]}-{n}"
                n += 1
            used.add(sheet)
            out = d.copy()
            out["Date"] = pd.to_datetime(out["Date"]).dt.date
            out.to_excel(xl, sheet_name=sheet, index=False)
            ws = xl.sheets[sheet]
            ws.freeze_panes = "A2"
            for i, col in enumerate(out.columns, start=1):
                letter = ws.cell(row=1, column=i).column_letter
                ws.column_dimensions[letter].width = 52 if col == "Description" else 14
    return buf.getvalue()


a1, a2, a3 = st.columns(3)
a1.download_button("All Excel files (.zip)", zip_of("xlsx"),
                   f"statements-excel-{stamp}.zip", "application/zip",
                   use_container_width=True, key="zip-xlsx")
a2.download_button(f"All CSV files (.zip)", zip_of("csv"),
                   f"statements-csv-{stamp}.zip", "application/zip",
                   use_container_width=True, key="zip-csv")
a3.download_button("One workbook, a sheet per file", one_workbook(),
                   f"statements-combined-{stamp}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   use_container_width=True, key="combined")

st.caption("Each download reflects any edits you made in that file's table.")
