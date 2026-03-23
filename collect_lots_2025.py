#!/usr/bin/env python3
"""Collect 2025 lots by TRU codes from goszakup and build output CSV.

The script:
1) Reads TRU codes from a source Google Sheet.
2) Scrapes goszakup lots with filters from the task example.
3) Reads an existing target Google Sheet and skips already loaded rows.
4) Writes only new rows to a CSV in the required output structure.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GID = "0"
GOSZAKUP_LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"

DEFAULT_STATUS = "360"  # "Закупка состоялась"
DEFAULT_AMOUNT_FROM = "15000000"
DEFAULT_YEAR = "2025"
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT_SEC = 45
DEFAULT_RETRIES = 5
DEFAULT_PAGE_SIZE = 40

OUTPUT_HEADER = [
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

SHOWING_RE = re.compile(
    r"Показано\s*c\s*([\d\s]+)\s*по\s*([\d\s]+)\s*из\s*([\d\s]+)\s*записей",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


@dataclass
class LotRecord:
    lot_no: str
    tru_code: str
    tru_name: str
    announce_name: str
    lot_name_desc: str
    quantity: str
    amount: str
    method: str
    status: str

    def to_row(self) -> list[str]:
        return [
            self.lot_no,
            self.tru_code,
            self.tru_name,
            self.announce_name,
            self.lot_name_desc,
            self.quantity,
            self.amount,
            self.method,
            self.status,
        ]


def csv_export_url(sheet_id: str, gid: str = GID) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def download_text(url: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> str:
    with urllib.request.urlopen(url, timeout=timeout_sec) as response:
        return response.read().decode("utf-8-sig")


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def read_source_codes(sheet_id: str) -> list[TruCode]:
    text = download_text(csv_export_url(sheet_id))
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []

    header = [h.strip() for h in rows[0]]
    if len(header) < 2:
        raise ValueError("Unexpected source sheet structure: expected at least 2 columns")

    result: list[TruCode] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if not row:
            continue
        code = clean_text(row[0]) if len(row) > 0 else ""
        name = clean_text(row[1]) if len(row) > 1 else ""
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append(TruCode(code=code, name=name))
    return result


def read_existing_pairs(sheet_id: str) -> set[tuple[str, str]]:
    """Read existing (lot_no, tru_code) pairs from target sheet."""
    text = download_text(csv_export_url(sheet_id), timeout_sec=120)
    reader = csv.reader(io.StringIO(text))
    pairs: set[tuple[str, str]] = set()
    for idx, row in enumerate(reader):
        if idx == 0:
            continue
        if len(row) < 2:
            continue
        lot_no = clean_text(row[0])
        tru_code = clean_text(row[1])
        if lot_no and tru_code:
            pairs.add((lot_no, tru_code))
    return pairs


def extract_primary_cell_text(cell) -> str:
    first_strong = cell.select_one("a strong")
    if first_strong:
        return clean_text(first_strong.get_text(" ", strip=True))

    first_anchor = cell.find("a")
    if first_anchor:
        text = clean_text(first_anchor.get_text(" ", strip=True))
        if text:
            return text

    text = clean_text(cell.get_text(" ", strip=True))
    text = text.replace("История", "").strip()
    if "Заказчик:" in text:
        text = text.split("Заказчик:", 1)[0].strip()
    return text


def parse_total_records(html: str) -> int | None:
    match = SHOWING_RE.search(html)
    if not match:
        return None
    return int(match.group(3).replace(" ", ""))


def parse_lots(html: str, tru_code: str, tru_name: str) -> list[LotRecord]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="search-result")
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    records: list[LotRecord] = []
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 7:
            continue

        lot_no = clean_text(cells[0].get_text(" ", strip=True))
        if not lot_no or "ничего не найдено" in lot_no.lower():
            continue

        announce_name = extract_primary_cell_text(cells[1])
        lot_name_desc = extract_primary_cell_text(cells[2])
        quantity = clean_text(cells[3].get_text(" ", strip=True))
        amount = clean_text(cells[4].get_text(" ", strip=True))
        method = clean_text(cells[5].get_text(" ", strip=True))
        status = clean_text(cells[6].get_text(" ", strip=True))

        records.append(
            LotRecord(
                lot_no=lot_no,
                tru_code=tru_code,
                tru_name=tru_name,
                announce_name=announce_name,
                lot_name_desc=lot_name_desc,
                quantity=quantity,
                amount=amount,
                method=method,
                status=status,
            )
        )
    return records


def build_params(
    tru_code: str,
    status: str,
    amount_from: str,
    year: str,
    page: int,
) -> dict[str, str]:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": tru_code,
        "filter[status][]": status,
        "filter[customer]": "",
        "filter[amount_from]": amount_from,
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": year,
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
    }
    if page > 1:
        params["page"] = str(page)
    return params


def get_html_with_retries(
    session: requests.Session,
    params: dict[str, str],
    timeout_sec: int,
    retries: int,
) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(
                GOSZAKUP_LOTS_URL,
                params=params,
                timeout=timeout_sec,
                headers={"User-Agent": "Mozilla/5.0 (compatible; lot-collector/1.0)"},
            )
            if response.status_code >= 500:
                raise requests.HTTPError(
                    f"Server error {response.status_code} for {response.url}",
                    response=response,
                )
            response.raise_for_status()
            return response.text
        except Exception as error:  # noqa: BLE001
            last_error = error
            sleep_for = min(30, 2**attempt)
            time.sleep(sleep_for)
    if last_error is None:
        raise RuntimeError("Unknown network error while requesting goszakup")
    raise RuntimeError(str(last_error)) from last_error


def scrape_code(
    tru: TruCode,
    status: str,
    amount_from: str,
    year: str,
    timeout_sec: int,
    retries: int,
    max_pages: int,
    log_every_pages: int,
    existing_pairs: set[tuple[str, str]],
    print_lock: threading.Lock,
) -> list[LotRecord]:
    session = requests.Session()
    page = 1
    total_records: int | None = None
    collected: list[LotRecord] = []
    seen_pairs: set[tuple[str, str]] = set()
    previous_signature: tuple[str, str, int] | None = None
    duplicate_page_hits = 0
    page_size_detected: int | None = None
    empty_page_retries = 0

    while True:
        if page > max_pages:
            with print_lock:
                print(
                    f"[warn] code={tru.code} reached max_pages={max_pages}, "
                    "stopping pagination to avoid infinite loop",
                    flush=True,
                )
            break
        if log_every_pages > 0 and page % log_every_pages == 0:
            with print_lock:
                print(f"[page] code={tru.code} page={page}", flush=True)

        params = build_params(
            tru_code=tru.code,
            status=status,
            amount_from=amount_from,
            year=year,
            page=page,
        )
        html = get_html_with_retries(
            session=session,
            params=params,
            timeout_sec=timeout_sec,
            retries=retries,
        )
        if total_records is None:
            total_records = parse_total_records(html)

        rows = parse_lots(html, tru_code=tru.code, tru_name=tru.name)
        if not rows:
            if total_records is not None and page > 1:
                effective_page_size = page_size_detected or DEFAULT_PAGE_SIZE
                estimated_total_pages = max(1, math.ceil(total_records / effective_page_size))
                if page < estimated_total_pages and empty_page_retries < 2:
                    empty_page_retries += 1
                    time.sleep(empty_page_retries)
                    continue
            break
        empty_page_retries = 0
        if page_size_detected is None:
            page_size_detected = max(1, len(rows))

        first_lot = rows[0].lot_no
        last_lot = rows[-1].lot_no
        signature = (first_lot, last_lot, len(rows))
        if signature == previous_signature:
            duplicate_page_hits += 1
        else:
            duplicate_page_hits = 0
            previous_signature = signature
        if duplicate_page_hits >= 2:
            with print_lock:
                print(
                    f"[warn] code={tru.code} repeating same page data "
                    f"(signature={signature}), stopping pagination",
                    flush=True,
                )
            break

        for row in rows:
            pair = (row.lot_no, row.tru_code)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if pair in existing_pairs:
                continue
            collected.append(row)

        if total_records is None:
            if len(rows) < DEFAULT_PAGE_SIZE:
                break
            page += 1
            continue

        effective_page_size = page_size_detected or DEFAULT_PAGE_SIZE
        total_pages = max(1, math.ceil(total_records / effective_page_size))
        if page >= total_pages:
            break
        page += 1

    with print_lock:
        print(
            f"[done] code={tru.code} pages={page} total_found={len(seen_pairs)} new={len(collected)}",
            flush=True,
        )
    return collected


def write_rows(path: Path, rows: Iterable[LotRecord]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(OUTPUT_HEADER)
        for row in rows:
            writer.writerow(row.to_row())
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--target-sheet-id", default=TARGET_SHEET_ID)
    parser.add_argument("--year", default=DEFAULT_YEAR)
    parser.add_argument("--status", default=DEFAULT_STATUS)
    parser.add_argument("--amount-from", default=DEFAULT_AMOUNT_FROM)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=60,
        help="Safety cap for pages per TRU code to avoid infinite loops.",
    )
    parser.add_argument(
        "--log-every-pages",
        type=int,
        default=0,
        help="Verbose page-level logging frequency (0 = off).",
    )
    parser.add_argument(
        "--output",
        default="output/lots_2025_new_rows.csv",
        help="Path to output CSV with new rows only.",
    )
    parser.add_argument(
        "--skip-existing-check",
        action="store_true",
        help="Do not read target sheet, export all found rows.",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="For testing: process only first N TRU codes (0 = all).",
    )
    parser.add_argument(
        "--only-code",
        action="append",
        default=[],
        help="Process only specific TRU code(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--only-codes-file",
        default="",
        help="Text file with TRU codes (one per line) to process exclusively.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("[step] reading source TRU codes...", flush=True)
    source_codes = read_source_codes(args.source_sheet_id)
    selected: set[str] = set()
    if args.only_code:
        selected.update(clean_text(code) for code in args.only_code if clean_text(code))
    if args.only_codes_file:
        file_codes = Path(args.only_codes_file).read_text(encoding="utf-8").splitlines()
        selected.update(clean_text(code) for code in file_codes if clean_text(code))
    if selected:
        source_codes = [item for item in source_codes if item.code in selected]
    if args.limit_codes > 0:
        source_codes = source_codes[: args.limit_codes]
    print(f"[info] loaded TRU codes: {len(source_codes)}", flush=True)
    if not source_codes:
        print("[warn] no TRU codes found, nothing to do", flush=True)
        return 0

    existing_pairs: set[tuple[str, str]] = set()
    if args.skip_existing_check:
        print("[info] skip existing check enabled", flush=True)
    else:
        print("[step] reading target sheet for deduplication...", flush=True)
        existing_pairs = read_existing_pairs(args.target_sheet_id)
        print(f"[info] existing lot+TRU pairs: {len(existing_pairs)}", flush=True)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        "[step] scraping goszakup "
        f"(year={args.year}, status={args.status}, amount_from={args.amount_from})...",
        flush=True,
    )
    print_lock = threading.Lock()
    all_new_rows: list[LotRecord] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                scrape_code,
                tru,
                args.status,
                args.amount_from,
                args.year,
                args.timeout_sec,
                args.retries,
                args.max_pages,
                args.log_every_pages,
                existing_pairs,
                print_lock,
            )
            for tru in source_codes
        ]
        for idx, future in enumerate(as_completed(futures), start=1):
            rows = future.result()
            all_new_rows.extend(rows)
            if idx % 50 == 0 or idx == len(futures):
                print(
                    f"[progress] processed codes: {idx}/{len(futures)}, "
                    f"accumulated new rows: {len(all_new_rows)}",
                    flush=True,
                )

    # Keep output stable for easier comparison/import.
    all_new_rows.sort(key=lambda r: (r.tru_code, r.lot_no))
    written = write_rows(output_path, all_new_rows)
    print(f"[done] output written: {output_path} (rows: {written})", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[stop] interrupted by user", file=sys.stderr)
        raise SystemExit(130)
