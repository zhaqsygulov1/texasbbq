#!/usr/bin/env python3
"""Upload a large CSV into Google Sheets using overlapping chunks.

This mode avoids navigating to non-existent rows (which gets clipped by Sheets).
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path
from typing import List, Tuple

from playwright.sync_api import sync_playwright


DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL)
    parser.add_argument("--input-csv", default="output/lots_2025_final.csv")
    parser.add_argument("--chunk-size", type=int, default=120000)
    parser.add_argument("--wait-ms", type=int, default=1500)
    parser.add_argument("--post-chunk-wait-sec", type=int, default=20)
    parser.add_argument("--save-wait-sec", type=int, default=60)
    parser.add_argument("--skip-clear", action="store_true")
    return parser.parse_args()


def load_rows(csv_path: Path) -> List[List[str]]:
    rows: List[List[str]] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader):
            if idx > 0 and len(row) > 1 and row[1] and not row[1].startswith("'"):
                row[1] = "'" + row[1]
            rows.append(row)
    return rows


def build_overlapping_chunks(rows: List[List[str]], chunk_size: int) -> List[Tuple[int, str, int]]:
    """Return list of (target_row, tsv_text, logical_start_index)."""
    chunks: List[Tuple[int, str, int]] = []
    n = len(rows)
    start = 0
    while start < n:
        end = min(n, start + chunk_size)
        if start == 0:
            paste_rows = rows[start:end]
            target_row = 1
        else:
            paste_rows = rows[start - 1 : end]  # overlap one row to preserve continuity
            target_row = start
        tsv = "\n".join("\t".join(r) for r in paste_rows)
        chunks.append((target_row, tsv, start))
        start = end
    return chunks


def main() -> int:
    args = parse_args()
    input_csv = Path(args.input_csv)
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    print("Loading CSV rows...")
    rows = load_rows(input_csv)
    print(f"Loaded {len(rows)} rows.")
    chunks = build_overlapping_chunks(rows, args.chunk_size)
    print(f"Prepared {len(chunks)} overlapping chunks.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            permissions=["clipboard-read", "clipboard-write"],
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        print("Opening sheet...")
        page.goto(args.sheet_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#t-name-box", state="visible", timeout=120000)
        page.wait_for_timeout(8000)

        def goto_cell(cell_ref: str) -> None:
            box = page.locator("#t-name-box")
            for _ in range(3):
                box.click(force=True)
                box.fill(cell_ref)
                page.keyboard.press("Enter")
                page.wait_for_timeout(300)
                if box.input_value().strip().upper() == cell_ref.upper():
                    break
            page.mouse.click(95, 146)
            page.wait_for_timeout(150)

        def paste_text(tsv: str) -> None:
            page.evaluate("txt => navigator.clipboard.writeText(txt)", tsv)
            page.keyboard.press("Control+v")
            page.wait_for_timeout(args.wait_ms)

        if not args.skip_clear:
            print("Clearing existing content...")
            goto_cell("A1")
            page.keyboard.press("Control+a")
            page.keyboard.press("Control+a")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(2000)

        for i, (target_row, tsv, logical_start) in enumerate(chunks, start=1):
            goto_cell(f"A{target_row}")
            paste_text(tsv)
            line_count = tsv.count("\n") + 1
            print(
                f"Chunk {i}/{len(chunks)} pasted at A{target_row} "
                f"({line_count} rows, source start index {logical_start})."
            )
            # Let Sheets apply and sync large paste before next navigation.
            time.sleep(args.post_chunk_wait_sec)

        print("Waiting for autosave...")
        time.sleep(args.save_wait_sec)
        context.close()
        browser.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
