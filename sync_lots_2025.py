#!/usr/bin/env python3
"""Incrementally sync 2025 lots by TRU codes into a Google Sheet."""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GOSZAKUP_LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"
FULL_TRU_CODE_RE = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")

COUNT_RECORD = 500
MAX_GOSZAKUP_RECORDS_PER_CODE = 10_000
WORKER_DELAY_SECONDS = 0.2

thread_local = threading.local()

# Known spreadsheet formatting corruption in source table.
MANUAL_CODE_FIXES = {
    "12.3.6": "000012.300.600000",
}


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


class RateLimitError(RuntimeError):
    """Raised when goszakup returns a rate-limit page."""


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_tru_code(raw_code: str) -> str:
    code = normalize_spaces(raw_code)
    if not code:
        return ""
    if code in MANUAL_CODE_FIXES:
        return MANUAL_CODE_FIXES[code]
    parts = [p for p in code.split(".") if p != ""]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0].zfill(6)}.{parts[1].zfill(3)}.{parts[2].zfill(6)}"
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return f"{parts[0].zfill(6)}.{parts[1].zfill(3)}.000000"
    return code


def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def get_thread_session() -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = build_session()
        thread_local.session = session
    return session


def fetch_sheet_csv(sheet_id: str, gid: int = 0, timeout: int = 180) -> str:
    session = get_thread_session()
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    response = session.get(url, params={"format": "csv", "gid": gid}, timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def load_source_codes() -> list[TruCode]:
    raw_csv = fetch_sheet_csv(SOURCE_SHEET_ID, gid=0)
    reader = csv.reader(io.StringIO(raw_csv))
    next(reader, None)  # header
    by_code: dict[str, str] = {}
    skipped = 0
    duplicates = 0
    for row in reader:
        if not row:
            continue
        code = normalize_tru_code(row[0] if len(row) > 0 else "")
        name = normalize_spaces(row[1] if len(row) > 1 else "")
        if not code:
            continue
        if not FULL_TRU_CODE_RE.match(code):
            skipped += 1
            print(f"[warn] skipped malformed code: {row[0]!r} -> {code!r}", flush=True)
            continue
        if code in by_code:
            duplicates += 1
            continue
        by_code[code] = name
    if skipped:
        print(f"[warn] malformed source codes skipped: {skipped}", flush=True)
    if duplicates:
        print(f"[warn] duplicate source codes skipped: {duplicates}", flush=True)
    return [TruCode(code=code, name=name) for code, name in by_code.items()]


def load_existing_rows() -> tuple[set[tuple[str, str]], int, int]:
    """Returns: (existing_keys, non_empty_rows_count, total_rows_count_without_header)."""
    raw_csv = fetch_sheet_csv(TARGET_SHEET_ID, gid=0, timeout=240)
    reader = csv.reader(io.StringIO(raw_csv))
    next(reader, None)  # header

    keys: set[tuple[str, str]] = set()
    non_empty_row_count = 0
    total_row_count = 0
    for row in reader:
        total_row_count += 1
        if not any(normalize_spaces(c) for c in row):
            continue
        non_empty_row_count += 1
        lot_no = normalize_spaces(row[0] if len(row) > 0 else "")
        code = normalize_tru_code(row[1] if len(row) > 1 else "")
        if lot_no and code:
            keys.add((lot_no, code))
    return keys, non_empty_row_count, total_row_count


def extract_total_records(soup: BeautifulSoup) -> int:
    heading = soup.select_one("div.panel-heading")
    if not heading:
        return 0
    text = normalize_spaces(heading.get_text(" ", strip=True))
    match = re.search(r"из\s+([\d\s]+)\s+запис", text, re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_lot_table_rows(soup: BeautifulSoup, code: str, tru_name: str) -> list[list[str]]:
    rows = []
    for tr in soup.select("#search-result tbody tr"):
        cols = [normalize_spaces(td.get_text(" ", strip=True)) for td in tr.select("td")]
        if len(cols) < 7:
            continue
        lot_no, anno_name, lot_name_and_desc, qty, amount, method, status = cols[:7]
        if not lot_no:
            continue
        rows.append(
            [
                lot_no,
                code,
                tru_name,
                anno_name,
                lot_name_and_desc,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows


def fetch_lot_page(
    code: str,
    year: int,
    page: int,
    count_record: int,
    amount_from: str,
    statuses: list[str],
) -> BeautifulSoup:
    session = get_thread_session()
    params: dict[str, str | list[str]] = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "filter[amount_from]": amount_from,
        "filter[status][]": statuses,
        "smb": "",
        "count_record": str(count_record),
        "page": str(page),
    }

    for attempt in range(1, 11):
        try:
            response = session.get(GOSZAKUP_LOTS_URL, params=params, timeout=60)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            title = normalize_spaces(soup.title.get_text(" ", strip=True) if soup.title else "")
            if "429 Too Many Requests" in title:
                raise RateLimitError(f"429 page for {code} page {page}")
            if "Реестр лотов" not in title and "Портал государственных закупок" not in title:
                raise RuntimeError(f"Unexpected page title for {code} page {page}: {title}")
            return soup
        except Exception:
            if attempt >= 10:
                raise
            # Backoff is intentionally long because goszakup throttles aggressively.
            time.sleep(min(30, attempt * 2.5))
    raise RuntimeError(f"Cannot fetch page for {code} page {page}")


def scrape_code_lots(
    code: str,
    tru_name: str,
    year: int,
    count_record: int,
    amount_from: str,
    statuses: list[str],
) -> list[list[str]]:
    soup = fetch_lot_page(
        code=code,
        year=year,
        page=1,
        count_record=count_record,
        amount_from=amount_from,
        statuses=statuses,
    )
    total = extract_total_records(soup)

    # The source website limits to 10k rows per query for a code.
    total = min(total, MAX_GOSZAKUP_RECORDS_PER_CODE)
    if total <= 0:
        return []

    pages = math.ceil(total / count_record)
    result = parse_lot_table_rows(soup, code=code, tru_name=tru_name)

    for page in range(2, pages + 1):
        page_soup = fetch_lot_page(
            code=code,
            year=year,
            page=page,
            count_record=count_record,
            amount_from=amount_from,
            statuses=statuses,
        )
        result.extend(parse_lot_table_rows(page_soup, code=code, tru_name=tru_name))

    return result


def chunks(iterable: list[list[str]], size: int) -> Iterable[list[list[str]]]:
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def tsv_payload(rows: list[list[str]]) -> str:
    sanitized = []
    for row in rows:
        sanitized_row = [
            normalize_spaces(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
            for value in row
        ]
        sanitized.append("\t".join(sanitized_row))
    return "\n".join(sanitized)


def append_rows_to_target_sheet(rows: list[list[str]], start_row: int, chunk_size: int) -> None:
    if not rows:
        return

    url = f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/edit?usp=sharing"
    current_row = start_row

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://docs.google.com")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=180000)
        page.wait_for_timeout(12000)

        for index, chunk in enumerate(chunks(rows, chunk_size), start=1):
            target_cell = f"A{current_row}"
            payload = tsv_payload(chunk)

            page.click("#t-name-box")
            page.keyboard.press("Control+A")
            page.keyboard.type(target_cell)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1200)

            page.evaluate("text => navigator.clipboard.writeText(text)", payload)
            page.keyboard.press("Control+V")
            page.wait_for_timeout(max(3000, len(chunk) * 15))

            print(
                f"[upload] chunk={index} rows={len(chunk)} "
                f"target_cell={target_cell} next_row={current_row + len(chunk)}",
                flush=True,
            )
            current_row += len(chunk)

        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync 2025 lots by TRU codes to Google Sheet")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-codes", type=int, default=0, help="0 means all codes")
    parser.add_argument("--count-record", type=int, default=COUNT_RECORD)
    parser.add_argument(
        "--amount-from",
        type=str,
        default="15000000",
        help="Lower amount filter passed to goszakup (empty string disables filter)",
    )
    parser.add_argument(
        "--status",
        action="append",
        default=["360"],
        help="Status code filter; repeat flag for multiple values (default: 360)",
    )
    parser.add_argument("--chunk-size", type=int, default=400)
    parser.add_argument("--dry-run", action="store_true", help="Do not upload to target sheet")
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("/workspace/new_lots_2025.csv"),
        help="Where to save newly found rows",
    )
    args = parser.parse_args()

    print("[step] loading source TRU codes...", flush=True)
    tru_codes = load_source_codes()
    if args.max_codes > 0:
        tru_codes = tru_codes[: args.max_codes]
    print(f"[info] source_codes={len(tru_codes)}", flush=True)

    print("[step] loading existing target rows...", flush=True)
    existing_keys, existing_nonempty_rows, existing_total_rows = load_existing_rows()
    print(
        f"[info] existing_nonempty_rows={existing_nonempty_rows} "
        f"existing_total_rows={existing_total_rows} "
        f"existing_keys={len(existing_keys)}",
        flush=True,
    )

    new_rows: list[list[str]] = []
    new_keys: set[tuple[str, str]] = set()
    failed_items: list[TruCode] = []

    print("[step] scraping goszakup...", flush=True)
    print(
        f"[info] filters: amount_from={args.amount_from!r} "
        f"statuses={args.status}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(
                scrape_code_lots,
                item.code,
                item.name,
                args.year,
                args.count_record,
                args.amount_from,
                args.status,
            ): item
            for item in tru_codes
        }
        done = 0
        total = len(future_map)

        for future in as_completed(future_map):
            item = future_map[future]
            done += 1
            try:
                rows = future.result()
                accepted = 0
                for row in rows:
                    key = (normalize_spaces(row[0]), normalize_tru_code(row[1]))
                    if key in existing_keys or key in new_keys:
                        continue
                    new_keys.add(key)
                    new_rows.append(row)
                    accepted += 1
                print(
                    f"[scrape] {done}/{total} code={item.code} "
                    f"rows={len(rows)} accepted={accepted} total_new={len(new_rows)}",
                    flush=True,
                )
                time.sleep(WORKER_DELAY_SECONDS)
            except Exception as exc:
                print(f"[error] code={item.code} failed: {exc}", flush=True)
                failed_items.append(item)

    if failed_items:
        print(
            f"[step] retrying failed codes sequentially: {len(failed_items)}",
            flush=True,
        )
        still_failed = 0
        for idx, item in enumerate(failed_items, start=1):
            try:
                rows = scrape_code_lots(
                    item.code,
                    item.name,
                    args.year,
                    args.count_record,
                    args.amount_from,
                    args.status,
                )
                accepted = 0
                for row in rows:
                    key = (normalize_spaces(row[0]), normalize_tru_code(row[1]))
                    if key in existing_keys or key in new_keys:
                        continue
                    new_keys.add(key)
                    new_rows.append(row)
                    accepted += 1
                print(
                    f"[retry] {idx}/{len(failed_items)} code={item.code} "
                    f"rows={len(rows)} accepted={accepted} total_new={len(new_rows)}",
                    flush=True,
                )
                time.sleep(1.0)
            except Exception as exc:
                still_failed += 1
                print(f"[retry-error] code={item.code} failed: {exc}", flush=True)
                time.sleep(2.0)
        if still_failed:
            print(f"[warn] unresolved failed codes: {still_failed}", flush=True)

    print("[step] writing local CSV artifact...", flush=True)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
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
        )
        writer.writerows(new_rows)
    print(f"[info] local_csv={args.output_csv} new_rows={len(new_rows)}", flush=True)

    if args.dry_run:
        print("[done] dry-run mode, upload skipped", flush=True)
        return

    if not new_rows:
        print("[done] no new rows to upload", flush=True)
        return

    # Use physical row count (including blanks) to avoid overwriting existing data.
    start_row = existing_total_rows + 2  # +1 for header, +1 for next empty row.
    print(
        f"[step] uploading rows to target sheet from row {start_row} "
        f"chunk_size={args.chunk_size}",
        flush=True,
    )
    append_rows_to_target_sheet(new_rows, start_row=start_row, chunk_size=args.chunk_size)
    print("[done] upload completed", flush=True)


if __name__ == "__main__":
    main()
