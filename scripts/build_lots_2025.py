#!/usr/bin/env python3
"""Build lots list for 2025 by TRU codes from a Google Sheet."""

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
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEFAULT_OUTPUT = Path("data/lots_2025.csv")
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
REQUEST_TIMEOUT = 40
MAX_RETRIES = 4
COUNT_RECORD = 2000

CSV_HEADERS = [
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


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def decode_sheet_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_total_records(page_text: str) -> int:
    match = re.search(r"Показано\s*c\s*\d+\s*по\s*\d+\s*из\s*(\d+)", page_text)
    if not match:
        return 0
    return int(match.group(1))


def clean_announce_name(value: str) -> str:
    value = normalize_whitespace(value)
    if "Заказчик:" in value:
        value = value.split("Заказчик:", 1)[0]
    return normalize_whitespace(value)


def clean_lot_name(value: str) -> str:
    value = normalize_whitespace(value)
    if "История" in value:
        value = value.split("История", 1)[0]
    return normalize_whitespace(value)


class SearchResultParser(HTMLParser):
    """Parses #search-result table rows from goszakup search HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.in_result_table = False
        self.in_tbody = False
        self.in_tr = False
        self.in_cell = False
        self.current_cell_parts: List[str] = []
        self.current_row: List[str] = []
        self.rows: List[List[str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "table" and attr_map.get("id") == "search-result":
            self.in_result_table = True
            return
        if not self.in_result_table:
            return
        if tag == "tbody":
            self.in_tbody = True
        elif self.in_tbody and tag == "tr":
            self.in_tr = True
            self.current_row = []
        elif self.in_tr and tag in ("td", "th"):
            self.in_cell = True
            self.current_cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_result_table:
            self.in_result_table = False
            self.in_tbody = False
            self.in_tr = False
            self.in_cell = False
            return
        if not self.in_result_table:
            return
        if tag == "tbody":
            self.in_tbody = False
        elif tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            cell_text = normalize_whitespace("".join(self.current_cell_parts))
            self.current_row.append(cell_text)
        elif tag == "tr" and self.in_tr:
            self.in_tr = False
            if self.current_row:
                self.rows.append(self.current_row[:])


def parse_lot_rows(page_text: str) -> List[List[str]]:
    parser = SearchResultParser()
    parser.feed(page_text)
    parsed_rows: List[List[str]] = []
    for row in parser.rows:
        if len(row) < 7:
            continue
        if row[0] == "№ лота":
            continue
        parsed_rows.append(row[:7])
    return parsed_rows


@dataclass(frozen=True)
class TruItem:
    code: str
    name: str


def fetch_url_with_retries(
    session: requests.Session,
    url: str,
    params: Dict[str, str],
    timeout: int = REQUEST_TIMEOUT,
) -> str:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001 - keep retries broad for flaky endpoint
            last_error = exc
            sleep_seconds = 2**attempt
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Failed request after {MAX_RETRIES} retries: {url}") from last_error


def fetch_tru_codes(sheet_id: str) -> List[TruItem]:
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {"format": "csv"}
    response = requests.get(export_url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    csv_text = decode_sheet_bytes(response.content)
    reader = csv.DictReader(io.StringIO(csv_text))
    results: List[TruItem] = []
    for row in reader:
        code = normalize_whitespace((row.get("Код ТРУ") or ""))
        name = normalize_whitespace((row.get("Название") or ""))
        if not code:
            continue
        results.append(TruItem(code=code, name=name))
    return results


def base_search_params(code: str, page: int) -> Dict[str, str]:
    return {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[status][]": "360",
        "filter[customer]": "",
        "filter[amount_from]": "15000000",
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": "2025",
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": str(COUNT_RECORD),
        "page": str(page),
    }


_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
                )
            }
        )
        _thread_local.session = session
    return session


def fetch_rows_for_tru(tru: TruItem) -> List[Dict[str, str]]:
    session = get_thread_session()
    first_page_text = fetch_url_with_retries(session, SEARCH_URL, base_search_params(tru.code, page=1))
    total = parse_total_records(first_page_text)
    pages = max(1, math.ceil(total / COUNT_RECORD))

    parsed = parse_lot_rows(first_page_text)
    rows: List[List[str]] = parsed[:]
    for page in range(2, pages + 1):
        page_text = fetch_url_with_retries(session, SEARCH_URL, base_search_params(tru.code, page=page))
        rows.extend(parse_lot_rows(page_text))

    normalized_rows: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str]] = set()
    for row in rows:
        lot_number, announce_name, lot_name, qty, amount, method, status = row
        lot_number = normalize_whitespace(lot_number)
        announce_name = clean_announce_name(announce_name)
        lot_name = clean_lot_name(lot_name)
        qty = normalize_whitespace(qty)
        amount = normalize_whitespace(amount)
        method = normalize_whitespace(method)
        status = normalize_whitespace(status)

        if not lot_number:
            continue

        dedupe_key = (tru.code, lot_number, announce_name)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        normalized_rows.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": tru.code,
                "Наименование товара": tru.name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_name,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": method,
                "Статус": status,
            }
        )
    return normalized_rows


def write_csv(rows: Iterable[Dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_dataset(source_sheet_id: str, output: Path, workers: int, limit: int | None) -> None:
    codes = fetch_tru_codes(source_sheet_id)
    if limit is not None:
        codes = codes[:limit]
    print(f"Loaded TRU codes: {len(codes)}")

    all_rows: List[Dict[str, str]] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_rows_for_tru, code): code for code in codes}
        for future in as_completed(future_map):
            tru = future_map[future]
            completed += 1
            try:
                rows = future.result()
                all_rows.extend(rows)
                print(
                    f"[{completed}/{len(codes)}] {tru.code} -> {len(rows)} rows "
                    f"(total accumulated: {len(all_rows)})"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{completed}/{len(codes)}] {tru.code} failed: {exc}")

    all_rows.sort(key=lambda row: (row["Код ТРУ"], row["№ лота"]))
    write_csv(all_rows, output)
    print(f"Done. Total rows: {len(all_rows)}")
    print(f"Output file: {output.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build lots list by TRU codes (year=2025).")
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="Limit number of TRU codes for test runs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_dataset(
        source_sheet_id=args.source_sheet_id,
        output=Path(args.output),
        workers=args.workers,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
