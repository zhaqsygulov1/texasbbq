#!/usr/bin/env python3
"""Upload prepared lots CSV into target Google Sheet."""

from __future__ import annotations

import argparse
import csv
import io
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def load_non_empty_target_rows() -> int:
    url = f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/export"
    response = requests.get(url, params={"format": "csv", "gid": 0}, timeout=240)
    response.raise_for_status()
    response.encoding = "utf-8"

    reader = csv.reader(io.StringIO(response.text))
    next(reader, None)
    rows = 0
    for row in reader:
        if any(normalize_spaces(c) for c in row):
            rows += 1
    return rows


def load_rows_from_csv(path: Path) -> list[list[str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        rows = []
        for row in reader:
            if any(normalize_spaces(c) for c in row):
                rows.append(row[:9])
        return rows


def tsv_payload(rows: list[list[str]]) -> str:
    lines = []
    for row in rows:
        normalized = [
            normalize_spaces(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
            for value in row
        ]
        lines.append("\t".join(normalized))
    return "\n".join(lines)


def chunk_rows(rows: list[list[str]], size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def upload_rows(rows: list[list[str]], start_row: int, chunk_size: int) -> None:
    if not rows:
        print("[done] nothing to upload", flush=True)
        return

    url = f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/edit?gid=0#gid=0"
    current = start_row

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://docs.google.com")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=180000)
        page.wait_for_timeout(12000)

        for idx, chunk in enumerate(chunk_rows(rows, chunk_size), start=1):
            payload = tsv_payload(chunk)
            target_cell = f"A{current}"

            page.click("#t-name-box")
            page.keyboard.press("Control+A")
            page.keyboard.type(target_cell)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)

            page.evaluate("text => navigator.clipboard.writeText(text)", payload)
            page.keyboard.press("Control+V")
            # Large chunks need extra time for Sheets to process.
            page.wait_for_timeout(max(4000, len(chunk) * 8))

            print(
                f"[upload] chunk={idx} rows={len(chunk)} cell={target_cell} "
                f"next={current + len(chunk)}",
                flush=True,
            )
            current += len(chunk)

        browser.close()

    print("[done] upload completed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload lots CSV into target Google Sheet")
    parser.add_argument("--csv", type=Path, required=True, help="Path to prepared CSV file")
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--start-row", type=int, default=0, help="If 0, auto-detect")
    args = parser.parse_args()

    rows = load_rows_from_csv(args.csv)
    print(f"[info] rows_from_csv={len(rows)} file={args.csv}", flush=True)
    if not rows:
        print("[done] empty CSV", flush=True)
        return

    start_row = args.start_row
    if start_row <= 0:
        existing = load_non_empty_target_rows()
        start_row = existing + 2
        print(f"[info] auto start_row={start_row} (existing={existing})", flush=True)

    upload_rows(rows=rows, start_row=start_row, chunk_size=args.chunk_size)


if __name__ == "__main__":
    main()
