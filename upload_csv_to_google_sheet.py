#!/usr/bin/env python3
"""Upload a CSV to a Google Sheet tab via browser automation (anonymous session)."""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path
from typing import List

from playwright.sync_api import sync_playwright


DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    parser.add_argument("--input-csv", default="output/lots_2025_final.csv")
    parser.add_argument("--chunk-rows", type=int, default=2000)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--wait-ms", type=int, default=500)
    parser.add_argument("--save-wait-sec", type=int, default=25)
    return parser.parse_args()


def csv_to_tsv_chunks(csv_path: Path, chunk_rows: int) -> List[str]:
    chunks: List[str] = []
    current_rows: List[str] = []

    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader):
            # Preserve TRU code as text in Google Sheets.
            if idx > 0 and len(row) > 1 and row[1] and not row[1].startswith("'"):
                row[1] = "'" + row[1]
            current_rows.append("\t".join(row))
            if len(current_rows) >= chunk_rows:
                chunks.append("\n".join(current_rows))
                current_rows = []
    if current_rows:
        chunks.append("\n".join(current_rows))
    return chunks


def main() -> int:
    args = parse_args()
    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    print("Preparing TSV chunks from CSV...")
    chunks = csv_to_tsv_chunks(input_csv, args.chunk_rows)
    total_rows = 0
    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        total_rows = sum(1 for _ in f)
    print(f"Prepared {len(chunks)} chunks from {total_rows} CSV lines.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            permissions=["clipboard-read", "clipboard-write"],
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        print("Opening target Google Sheet...")
        page.goto(args.sheet_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#t-name-box", state="visible", timeout=120000)
        page.wait_for_timeout(8000)

        range_box = page.locator("#t-name-box")

        def goto_cell(cell_ref: str) -> None:
            for _ in range(3):
                range_box.click(force=True)
                range_box.fill(cell_ref)
                page.keyboard.press("Enter")
                page.wait_for_timeout(250)
                if range_box.input_value().strip().upper() == cell_ref.upper():
                    break
            # Move focus from name box to grid before paste.
            page.mouse.click(95, 146)
            page.wait_for_timeout(120)

        def paste_text(text: str) -> None:
            page.evaluate("txt => navigator.clipboard.writeText(txt)", text)
            page.keyboard.press("Control+v")
            page.wait_for_timeout(args.wait_ms)

        print("Clearing existing sheet data...")
        goto_cell("A1")
        page.keyboard.press("Control+a")
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(2000)

        start_row = 1
        for i, chunk in enumerate(chunks, start=1):
            goto_cell(f"A{start_row}")
            paste_text(chunk)
            lines_in_chunk = chunk.count("\n") + 1
            print(f"Chunk {i}/{len(chunks)} pasted at A{start_row} ({lines_in_chunk} rows).")
            start_row += lines_in_chunk

        print("Waiting for autosave...")
        time.sleep(args.save_wait_sec)

        context.close()
        browser.close()

    print("Upload completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
