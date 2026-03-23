#!/usr/bin/env python3
"""Append rows from new_rows_2025.csv into target Google Sheet via Playwright."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SOURCE_PATH = Path("/workspace/new_rows_2025.csv")
CHECKPOINT_PATH = Path("/workspace/upload_checkpoint.json")

# One row has already been inserted manually in test phase:
# CSV row 2 -> sheet row 120002.
INITIAL_START_ROW = 120003
SKIP_DATA_ROWS = 1
CHUNK_SIZE = 10000


def clean_cell(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def iter_chunks():
    with SOURCE_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for _ in range(SKIP_DATA_ROWS):
            next(reader)

        chunk: list[list[str]] = []
        for row in reader:
            # Force text treatment in sheet to avoid date auto-conversion (e.g. 12.3.6).
            row[1] = "'" + clean_cell(row[1])
            normalized = [clean_cell(cell) for cell in row]
            chunk.append(normalized)
            if len(chunk) >= CHUNK_SIZE:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {"next_row": INITIAL_START_ROW, "chunks_done": 0, "rows_done": 0}


def save_checkpoint(next_row: int, chunks_done: int, rows_done: int) -> None:
    payload = {
        "next_row": next_row,
        "chunks_done": chunks_done,
        "rows_done": rows_done,
        "updated_at_epoch": int(time.time()),
    }
    CHECKPOINT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    state = load_checkpoint()
    next_row = int(state["next_row"])
    chunks_done = int(state["chunks_done"])
    rows_done = int(state["rows_done"])

    print(f"Resuming upload from row {next_row}, chunks_done={chunks_done}, rows_done={rows_done}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()

        for idx, chunk in enumerate(iter_chunks(), start=1):
            if idx <= chunks_done:
                continue

            tsv_text = "\n".join("\t".join(row) for row in chunk)
            url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0&range=A{next_row}"
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(10000)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            page.evaluate("(txt) => navigator.clipboard.writeText(txt)", tsv_text)
            page.keyboard.press("Control+V")

            # Large multi-row paste requires time for grid update/autosave.
            pause_ms = min(45000, 7000 + len(chunk) * 2)
            page.wait_for_timeout(pause_ms)

            next_row += len(chunk)
            chunks_done += 1
            rows_done += len(chunk)
            save_checkpoint(next_row=next_row, chunks_done=chunks_done, rows_done=rows_done)

            print(
                f"Uploaded chunk {chunks_done}: rows={len(chunk)}, "
                f"rows_done={rows_done}, next_row={next_row}"
            )

        browser.close()

    print("Upload completed.")


if __name__ == "__main__":
    main()
