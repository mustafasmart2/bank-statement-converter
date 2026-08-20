"""
app.py — drag-and-drop web UI.

Run it:   streamlit run app.py
Opens at: http://localhost:8501
"""

from __future__ import annotations

import io
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
    to_csv_bytes,
    to_excel_bytes,
)

st.set_page_config(page_title="Bank Statement Converter", page_icon="📄", layout="wide")

BLUE, GREEN = "#2D99D0", "#66B937"

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 2rem; max-width: 1400px; }}
      h1 {{ color: {BLUE}; font-size: 1.9rem !important; }}
      .stDownloadButton button {{ background: {GREEN}; color: white; border: 0; }}
      .stDownloadButton button:hover {{ background: #58a52f; color: white; }}
      div[data-testid="stMetricValue"] {{ font-size: 1.35rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Bank Statement Converter")
st.caption("PDF statements → Excel / CSV. Runs entirely on this machine — nothing is uploaded anywhere.")

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
    default_year = st.number_input(
        "Year to assume if the PDF omits it", min_value=2000, max_value=2100,
        value=datetime.now().year, step=1,
    )
    invert = st.checkbox("Flip debit / credit",
                         help="Tick this only if money-in and money-out come out the wrong way round.")
    flavour = st.selectbox("Export layout", EXPORT_FLAVOURS,
                           format_func=lambda s: s.title())
    st.divider()
    st.caption("Wrong output? Send the layout details to whoever maintains this "
               "and add the column heading to COLUMN_ALIASES in statement_parser.py.")

# ---------------------------------------------------------------- upload
files = st.file_uploader("Drop your PDF statements here", type=["pdf"], accept_multiple_files=True)

if not files:
    st.info("Upload one or more PDF bank statements to begin. Multiple files are merged into one sheet, "
            "with a **Source File** column so you can tell them apart.")
    st.stop()

opts = Options(dayfirst=dayfirst, default_year=int(default_year), invert=invert)

frames, logs, failures = [], [], []
progress = st.progress(0.0, text="Reading…")
for i, f in enumerate(files, start=1):
    progress.progress(i / len(files), text=f"Reading {f.name}…")
    parser = StatementParser(opts)
    try:
        df = parser.parse(io.BytesIO(f.getvalue()), password=password or None, source=f.name)
        if df.empty:
            failures.append((f.name, "No transactions found. The PDF may be a scan — see the README on OCR."))
        else:
            frames.append(df)
        logs.append((f.name, parser.log))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "password" in msg.lower() or "decrypt" in msg.lower():
            msg = "This PDF is password protected — enter the password in the sidebar."
        failures.append((f.name, msg))
progress.empty()

for name, msg in failures:
    st.error(f"**{name}** — {msg}")

if not frames:
    st.stop()

data = pd.concat(frames, ignore_index=True).sort_values(["Source File", "Date"]).reset_index(drop=True)
check = reconcile(data) if data["Source File"].nunique() == 1 else None

# ---------------------------------------------------------------- summary
c1, c2, c3, c4 = st.columns(4)
c1.metric("Transactions", len(data))
c2.metric("Money in", f"{data['Credit'].fillna(0).sum():,.2f}")
c3.metric("Money out", f"{data['Debit'].fillna(0).sum():,.2f}")
c4.metric("Net", f"{data['Amount'].fillna(0).sum():,.2f}")

if check:
    (st.success if check["ok"] else st.warning)(check["message"])
    if check["mismatches"]:
        with st.expander(f"Rows that don't tie to the running balance ({len(check['mismatches'])})"):
            st.dataframe(pd.DataFrame(check["mismatches"]), use_container_width=True)
            st.caption("Usually a wrapped description or a fee line the parser mis-read. "
                       "Fix it in the table below — the download uses your edits.")
else:
    st.info("Multiple statements loaded, so the running-balance check is skipped. "
            "Upload one file at a time to have every row verified.")

# ---------------------------------------------------------------- edit + export
st.subheader("Transactions")
st.caption("Edit any cell directly. Downloads below use whatever is in this table.")
edited = st.data_editor(
    data,
    use_container_width=True,
    hide_index=True,
    num_rows="dynamic",
    column_config={
        "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
        "Debit": st.column_config.NumberColumn("Debit", format="%.2f"),
        "Credit": st.column_config.NumberColumn("Credit", format="%.2f"),
        "Amount": st.column_config.NumberColumn("Amount", format="%.2f"),
        "Balance": st.column_config.NumberColumn("Balance", format="%.2f"),
    },
    height=440,
)

stamp = datetime.now().strftime("%Y%m%d-%H%M")
d1, d2, d3 = st.columns(3)
d1.download_button("Download Excel", to_excel_bytes(edited, reconcile(edited)),
                   f"statements-{stamp}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   use_container_width=True)
d2.download_button(f"Download CSV ({flavour})", to_accounting_csv(edited, flavour),
                   f"statements-{flavour}-{stamp}.csv", "text/csv", use_container_width=True)

if edited["Source File"].nunique() > 1:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, grp in edited.groupby("Source File"):
            z.writestr(f"{name.rsplit('.', 1)[0]}.csv", to_accounting_csv(grp, flavour))
    d3.download_button("Download one CSV per PDF (zip)", zbuf.getvalue(),
                       f"statements-{stamp}.zip", "application/zip", use_container_width=True)

with st.expander("How each page was read"):
    for name, entries in logs:
        st.markdown(f"**{name}**")
        for line in entries:
            st.text("  " + line)
