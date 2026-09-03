"""
app.py — drag-and-drop web UI.

Run it:   streamlit run app.py
Opens at: http://localhost:8501

Drop in one PDF or twenty. With more than one, you are asked whether to merge them
into a single table or keep each statement separate on its own tab.
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
      button[data-baseweb="tab"] {{ min-width: 3rem; justify-content: center; }}
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
    st.info("Upload one or more PDF statements. With more than one you'll be asked "
            "whether to merge them or keep them separate.")
    st.stop()

xl_date_fmt = "DD/MM/YYYY" if dayfirst else "MM/DD/YYYY"

opts = Options(dayfirst=dayfirst,
               default_year=int(default_year) if default_year else None,
               invert=invert, ocr=use_ocr)

# ---------------------------------------------------------------- parse
results: list[dict] = []
progress = st.progress(0.0, text="Reading…")
for i, f in enumerate(files, start=1):
    progress.progress(i / len(files), text=f"Reading {f.name}…")
    parser = StatementParser(opts)
    entry = {"name": f.name, "df": None, "error": None, "log": [], "bank": None, "ocr": False}
    try:
        df = parser.parse(io.BytesIO(f.getvalue()), password=password or None, source=f.name)
        entry.update(log=parser.log, bank=parser.bank, ocr=parser.used_ocr)
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
for r in (r for r in results if r["df"] is None):
    st.error(f"**{r['name']}** — {r['error']}")

if not good:
    st.stop()

for r in good:
    r["check"] = reconcile(r["df"])

stamp = datetime.now().strftime("%Y%m%d-%H%M")

# ---------------------------------------------------------------- merge or not
batch_key = tuple(sorted(r["name"] for r in good))
if len(good) == 1:
    merge = False
else:
    if st.session_state.get("batch_key") != batch_key:
        st.session_state.pop("merge", None)
        st.session_state["batch_key"] = batch_key

    if "merge" not in st.session_state:
        with st.container(border=True):
            st.subheader(f"{len(good)} statements read — {sum(len(r['df']) for r in good):,} transactions")
            st.write("Do you want to merge all transactions into **one** Excel / CSV file?")
            st.caption("Merged gives you a single table with a **Source File** column. "
                       "Separate gives each statement its own tab, its own balance check "
                       "and its own download.")
            yes, no, _ = st.columns([1, 1, 4])
            if yes.button("Yes, merge them", type="primary", use_container_width=True):
                st.session_state["merge"] = True
                st.rerun()
            if no.button("No, keep separate", use_container_width=True):
                st.session_state["merge"] = False
                st.rerun()
        st.stop()

    merge = st.session_state["merge"]
    label = "merged into one file" if merge else "kept separate"
    left, right = st.columns([5, 1])
    left.caption(f"{len(good)} statements, {label}.")
    if right.button("Change", use_container_width=True):
        del st.session_state["merge"]
        st.rerun()

# ================================================================ MERGED VIEW
if merge:
    data = pd.concat([r["df"] for r in good], ignore_index=True)
    data = data.sort_values(["Source File", "Date"], kind="stable").reset_index(drop=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transactions", f"{len(data):,}")
    c2.metric("Money in", f"{data['Credit'].fillna(0).sum():,.2f}")
    c3.metric("Money out", f"{data['Debit'].fillna(0).sum():,.2f}")
    c4.metric("Net", f"{data['Amount'].fillna(0).sum():,.2f}")

    failed = [r for r in good if not r["check"]["ok"]]
    if failed:
        st.warning("These statements have rows that don't tie to their running balance: "
                   + ", ".join(f"**{r['name']}** ({len(r['check']['mismatches'])})" for r in failed))
    else:
        st.success(f"Every row in all {len(good)} statements ties to its running balance.")

    if any(r["ocr"] for r in good):
        st.info("Read by OCR (no text layer): "
                + ", ".join(r["name"] for r in good if r["ocr"]))

    st.caption("The balance check runs per statement — a merged table has no single "
               "running balance, since each account has its own.")

    edited = st.data_editor(
        data, use_container_width=True, hide_index=True, num_rows="dynamic",
        key="editor-merged",
        column_config={
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Debit": st.column_config.NumberColumn("Debit", format="%.2f"),
            "Credit": st.column_config.NumberColumn("Credit", format="%.2f"),
            "Amount": st.column_config.NumberColumn("Amount", format="%.2f"),
            "Balance": st.column_config.NumberColumn("Balance", format="%.2f"),
            "Code": st.column_config.TextColumn("Code", width="small"),
        },
        height=480,
    )

    d1, d2, _ = st.columns([1, 1, 2])
    d1.download_button("Download Excel", to_excel_bytes(edited, reconcile(edited), date_format=xl_date_fmt),
                       f"statements-merged-{stamp}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
    d2.download_button(f"Download CSV ({flavour})", to_accounting_csv(edited, flavour, dayfirst),
                       f"statements-merged-{stamp}.csv", "text/csv",
                       use_container_width=True)
    st.stop()

# ================================================================ SEPARATE VIEW
if len(good) > 1:
    c1, c2, c3 = st.columns(3)
    c1.metric("Files", len(good))
    c2.metric("Transactions", f"{sum(len(r['df']) for r in good):,}")
    c3.metric("Passing the balance check",
              f"{sum(1 for r in good if r['check']['ok'])} of {len(good)}")
    st.divider()

tabs = st.tabs([str(i) for i in range(1, len(good) + 1)])

for tab, r in zip(tabs, good):
    with tab:
        df, check = r["df"], r["check"]
        stem = safe_name(r["name"])

        st.markdown(f"#### {r['name']}")
        bits = []
        if r["bank"]:
            bits.append(f"Bank: **{r['bank']}**")
        if check.get("opening") is not None:
            bits.append(f"Opening **{check['opening']:,.2f}** → closing **{check['closing']:,.2f}**")
        if bits:
            st.caption("  ·  ".join(bits))

        head = st.columns(4)
        head[0].metric("Transactions", len(df))
        head[1].metric("Money in", f"{df['Credit'].fillna(0).sum():,.2f}")
        head[2].metric("Money out", f"{df['Debit'].fillna(0).sum():,.2f}")
        head[3].metric("Net", f"{df['Amount'].fillna(0).sum():,.2f}")

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
        d1.download_button("Download Excel", to_excel_bytes(edited, reconcile(edited), date_format=xl_date_fmt),
                           f"{stem}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True, key=f"xlsx-{stem}")
        d2.download_button(f"Download CSV ({flavour})", to_accounting_csv(edited, flavour, dayfirst),
                           f"{stem}.csv", "text/csv",
                           use_container_width=True, key=f"csv-{stem}")

        with st.expander("How this PDF was read"):
            for line in r["log"]:
                st.text(line)

if len(good) == 1:
    st.stop()

# ---------------------------------------------------------------- download all
st.divider()
st.subheader("Download everything")

frames = {safe_name(r["name"]): r.get("edited", r["df"]) for r in good}


def zip_of(kind: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for stem, d in frames.items():
            if kind == "csv":
                z.writestr(f"{stem}.csv", to_accounting_csv(d, flavour, dayfirst))
            else:
                z.writestr(f"{stem}.xlsx", to_excel_bytes(d, reconcile(d), date_format=xl_date_fmt))
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
                cell = ws.cell(row=1, column=i)
                ws.column_dimensions[cell.column_letter].width = (
                    52 if col == "Description" else 14)
                if col == "Date":
                    for r in range(2, len(out) + 2):
                        ws.cell(row=r, column=i).number_format = xl_date_fmt
                elif col in {"Debit", "Credit", "Amount", "Balance"}:
                    for r in range(2, len(out) + 2):
                        ws.cell(row=r, column=i).number_format = "#,##0.00"
    return buf.getvalue()


a1, a2, a3 = st.columns(3)
a1.download_button("All Excel files (.zip)", zip_of("xlsx"),
                   f"statements-excel-{stamp}.zip", "application/zip",
                   use_container_width=True, key="zip-xlsx")
a2.download_button("All CSV files (.zip)", zip_of("csv"),
                   f"statements-csv-{stamp}.zip", "application/zip",
                   use_container_width=True, key="zip-csv")
a3.download_button("One workbook, a sheet per file", one_workbook(),
                   f"statements-combined-{stamp}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   use_container_width=True, key="combined")

st.caption("Each download reflects any edits you made in that file's table.")
