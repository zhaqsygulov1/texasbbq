#!/usr/bin/env python3
"""Build final 2025 lots table by TRU codes from Google Sheets CSV exports.

This script:
1) downloads source TRU codes sheet (code + name);
2) downloads destination/raw lots sheet;
3) keeps only rows whose TRU code exists in source sheet;
4) writes a normalized CSV with required columns;
5) writes a gzip-compressed copy for easier transfer.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

import requests

SRC_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DST_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"

FINAL_COLUMNS = [
    "№ лота",
    "Код ТРУ",
    "Наименование товара",
    "Наименование объявления",
    "Наименование и описание лота",
    "Кол-во",
    "Сумма, тг.",
    "Способ закупки",
    "Статус",
]


def sheet_export_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def download_csv(url: str, timeout: int = 120) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    # Google CSV exports are UTF-8 with optional BOM.
    return resp.content.decode("utf-8-sig", errors="replace")


def read_source_codes(csv_text: str) -> set[str]:
    codes: set[str] = set()
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        code = (row.get("Код ТРУ") or "").strip()
        if code:
            codes.add(code)
    return codes


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    lot_number = (row.get("№ лота") or row.get(" ") or "").strip()
    return {
        "№ лота": lot_number,
        "Код ТРУ": (row.get("Код ТРУ") or "").strip(),
        "Наименование товара": (row.get("Наименование товара") or "").strip(),
        "Наименование объявления": (row.get("Наименование объявления") or "").strip(),
        "Наименование и описание лота": (
            row.get("Наименование и описание лота") or ""
        ).strip(),
        "Кол-во": (row.get("Кол-во") or "").strip(),
        "Сумма, тг.": (row.get("Сумма, тг.") or "").strip(),
        "Способ закупки": (row.get("Способ закупки") or "").strip(),
        "Статус": (row.get("Статус") or "").strip(),
    }


def amount_to_float(value: str) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.,]", "", value).replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def write_final_csv(
    destination_rows_csv: str, source_codes: set[str], output_path: Path
) -> tuple[int, int, Counter[str], Counter[str], float | None]:
    reader = csv.DictReader(io.StringIO(destination_rows_csv))
    methods: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    matched_codes: set[str] = set()
    row_count = 0
    min_amount: float | None = None

    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FINAL_COLUMNS)
        writer.writeheader()

        for row in reader:
            code = (row.get("Код ТРУ") or "").strip()
            if code not in source_codes:
                continue
            normalized = normalize_row(row)
            writer.writerow(normalized)

            row_count += 1
            matched_codes.add(code)
            methods[normalized["Способ закупки"]] += 1
            statuses[normalized["Статус"]] += 1
            amount = amount_to_float(normalized["Сумма, тг."])
            if amount is not None:
                min_amount = amount if min_amount is None else min(min_amount, amount)

    return row_count, len(matched_codes), methods, statuses, min_amount


def gzip_file(path: Path) -> Path:
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    return gz_path


def main(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare final 2025 lots CSV by TRU code list."
    )
    parser.add_argument(
        "--output",
        default="lots_2025_tru_filtered.csv",
        help="Final CSV output file path.",
    )
    args = parser.parse_args(list(argv))

    out_path = Path(args.output).resolve()
    print("Downloading source TRU codes sheet...")
    src_csv = download_csv(sheet_export_url(SRC_SHEET_ID))
    source_codes = read_source_codes(src_csv)
    print(f"Loaded source TRU codes: {len(source_codes)}")

    print("Downloading destination lots sheet...")
    dst_csv = download_csv(sheet_export_url(DST_SHEET_ID))
    print("Building filtered final table...")
    rows, codes_with_rows, methods, statuses, min_amount = write_final_csv(
        dst_csv, source_codes, out_path
    )

    gz_path = gzip_file(out_path)
    print(f"Done. Output CSV: {out_path}")
    print(f"Compressed copy: {gz_path}")
    print(f"Rows written: {rows}")
    print(f"TRU codes with rows: {codes_with_rows}")
    print(f"Min amount found: {min_amount}")
    print("Top procurement methods:")
    for method, count in methods.most_common(10):
        print(f"  - {method}: {count}")
    print("Statuses:")
    for status, count in statuses.most_common():
        print(f"  - {status}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
