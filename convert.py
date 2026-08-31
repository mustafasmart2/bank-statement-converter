#!/usr/bin/env python3
"""
convert.py — batch convert statements from the terminal.

  python convert.py statement.pdf
  python convert.py inbox/*.pdf -o april.xlsx
  python convert.py locked.pdf --password 3105 --csv --flavour quickbooks
  python convert.py us_statement.pdf --month-first
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

from statement_parser import (
    EXPORT_FLAVOURS,
    Options,
    StatementParser,
    reconcile,
    to_accounting_csv,
    to_excel_bytes,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert PDF bank statements to Excel or CSV.")
    ap.add_argument("files", nargs="+", help="PDF files (wildcards allowed)")
    ap.add_argument("-o", "--output", help="output file (default: <first pdf name>.xlsx)")
    ap.add_argument("-p", "--password", help="PDF password, if the statement is locked")
    ap.add_argument("--csv", action="store_true", help="write CSV instead of Excel")
    ap.add_argument("--flavour", choices=EXPORT_FLAVOURS, default="standard",
                    help="CSV layout for your accounting package")
    ap.add_argument("--month-first", action="store_true", help="US dates (01/31/2026)")
    ap.add_argument("--invert", action="store_true", help="flip debit and credit")
    ap.add_argument("--year", type=int, help="year to assume when dates omit it "
                                             "(default: read it off the statement)")
    ap.add_argument("--no-ocr", action="store_true", help="skip OCR on image-only PDFs")
    ap.add_argument("--split", action="store_true", help="one output file per PDF")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    paths: list[Path] = []
    for pattern in args.files:
        hits = [Path(p) for p in glob.glob(pattern)]
        paths.extend(hits if hits else [Path(pattern)])
    paths = [p for p in paths if p.suffix.lower() == ".pdf"]
    if not paths:
        print("No PDF files matched.", file=sys.stderr)
        return 1

    opts = Options(dayfirst=not args.month_first, invert=args.invert,
                   default_year=args.year, ocr=not args.no_ocr)
    frames: list[tuple[Path, pd.DataFrame]] = []

    for path in paths:
        if not path.exists():
            print(f"  ! {path} not found", file=sys.stderr)
            continue
        parser = StatementParser(opts)
        try:
            df = parser.parse(str(path), password=args.password, source=path.name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {path.name}: {exc}", file=sys.stderr)
            continue
        if not args.quiet:
            print(f"  {path.name}")
            for line in parser.log:
                print(f"      {line}")
        if df.empty:
            print(f"  ! {path.name}: no transactions found (scanned PDF?)", file=sys.stderr)
            continue
        check = reconcile(df)
        flag = "ok " if check["ok"] else "!! "
        if not args.quiet:
            print(f"      {flag}{len(df)} transactions — {check['message']}")
        frames.append((path, df))

    if not frames:
        return 1

    def write(df: pd.DataFrame, out: Path) -> None:
        if args.csv:
            out = out.with_suffix(".csv")
            out.write_bytes(to_accounting_csv(df, args.flavour))
        else:
            out = out.with_suffix(".xlsx")
            out.write_bytes(to_excel_bytes(df, reconcile(df)))
        print(f"  -> {out}")

    if args.split:
        for path, df in frames:
            write(df, path.with_name(path.stem + "-converted"))
    else:
        merged = pd.concat([d for _, d in frames], ignore_index=True)
        merged = merged.sort_values(["Source File", "Date"]).reset_index(drop=True)
        default = frames[0][0].with_name(frames[0][0].stem + "-converted")
        write(merged, Path(args.output) if args.output else default)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
