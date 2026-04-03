#!/usr/bin/env python3
"""Collect 2025 goszakup lots by TRU codes from Google Sheets.

Reads TRU codes from a source Google Sheet and scrapes lot data from:
https://goszakup.gov.kz/ru/search/lots

Output format:
№ лота | Код ТРУ | Наименование товара | Наименование объявления |
Наименование и описание лота | Кол-во | Сумма, тг. | Способ закупки | Статус
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, List

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
OUTPUT_HEADERS = [
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

DEFAULT_FILTERS = {
    "year": "2025",
    "status": "360",  # Закупка состоялась
    "amount_from": "15000000",
}

COUNT_PER_PAGE_DEFAULT = 500
TOTAL_RE = re.compile(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей")
CAP_SPLIT_THRESHOLD_DEFAULT = 9998


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


@dataclass
class LotRow:
    lot_number: str
    tru_code: str
    product_name: str
    announcement_name: str
    lot_name_description: str
    quantity: str
    amount_kzt: str
    procurement_method: str
    status: str

    def as_csv_row(self) -> List[str]:
        return [
            self.lot_number,
            self.tru_code,
            self.product_name,
            self.announcement_name,
            self.lot_name_description,
            self.quantity,
            self.amount_kzt,
            self.procurement_method,
            self.status,
        ]


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def source_sheet_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid=0"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_tru_codes(session: requests.Session, sheet_id: str) -> List[TruCode]:
    resp = session.get(source_sheet_csv_url(sheet_id), timeout=40)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    rows = list(csv.reader(io.StringIO(resp.text)))
    if not rows:
        return []

    result: List[TruCode] = []
    for row in rows[1:]:
        if not row:
            continue
        code = normalize_ws(row[0]) if len(row) > 0 else ""
        name = normalize_ws(row[1]) if len(row) > 1 else ""
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    return result


def build_lots_url_params(
    tru_code: str,
    page: int,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    month: int | None = None,
) -> dict:
    params = {
        "filter[enstru]": tru_code,
        "filter[year]": year,
        "filter[status][]": status,
        "filter[amount_from]": amount_from,
        "count_record": str(count_per_page),
    }
    if month is not None:
        params["filter[month]"] = str(month)
    if page > 1:
        params["page"] = str(page)
    return params


def fetch_page_html(
    session: requests.Session,
    tru_code: str,
    page: int,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    month: int | None,
    retries: int = 3,
) -> str:
    url = "https://goszakup.gov.kz/ru/search/lots"
    params = build_lots_url_params(
        tru_code,
        page,
        year,
        status,
        amount_from,
        count_per_page,
        month,
    )
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=45)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp.text
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(1.2 * attempt + random.uniform(0.3, 1.0))
    return ""


def parse_total_records(html: str) -> int:
    match = TOTAL_RE.search(html)
    if not match:
        return 0
    value = match.group(1).replace(" ", "")
    return int(value) if value.isdigit() else 0


def parse_lot_rows_from_html(html: str, tru: TruCode) -> List[LotRow]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="search-result")
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    rows: List[LotRow] = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_cell = tds[0]
        anno_cell = tds[1]
        lot_desc_cell = tds[2]

        lot_number = ""
        lot_strong = lot_cell.find("strong")
        if lot_strong:
            lot_number = normalize_ws(lot_strong.get_text(" ", strip=True))
        if not lot_number:
            lot_text = normalize_ws(lot_cell.get_text(" ", strip=True))
            lot_match = re.search(r"\d+-[^\s]+", lot_text)
            lot_number = lot_match.group(0) if lot_match else lot_text

        announcement_name = ""
        anno_strong = anno_cell.find("strong")
        if anno_strong:
            announcement_name = normalize_ws(anno_strong.get_text(" ", strip=True))
        if not announcement_name:
            announcement_name = normalize_ws(anno_cell.get_text(" ", strip=True))

        lot_name_desc = ""
        desc_strong = lot_desc_cell.find("strong")
        if desc_strong:
            lot_name_desc = normalize_ws(desc_strong.get_text(" ", strip=True))
        if not lot_name_desc:
            lot_name_desc = normalize_ws(lot_desc_cell.get_text(" ", strip=True))

        quantity = normalize_ws(tds[3].get_text(" ", strip=True))
        amount = normalize_ws(tds[4].get_text(" ", strip=True))
        method = normalize_ws(tds[5].get_text(" ", strip=True))
        status = normalize_ws(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue
        rows.append(
            LotRow(
                lot_number=lot_number,
                tru_code=tru.code,
                product_name=tru.name,
                announcement_name=announcement_name,
                lot_name_description=lot_name_desc,
                quantity=quantity,
                amount_kzt=amount,
                procurement_method=method,
                status=status,
            )
        )
    return rows


def fetch_and_parse_page_rows(
    session: requests.Session,
    tru: TruCode,
    page: int,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    month: int | None = None,
    retries_on_empty: int = 3,
) -> tuple[str, List[LotRow]]:
    html = fetch_page_html(
        session=session,
        tru_code=tru.code,
        page=page,
        year=year,
        status=status,
        amount_from=amount_from,
        count_per_page=count_per_page,
        month=month,
    )
    rows = parse_lot_rows_from_html(html, tru)
    total_records = parse_total_records(html)
    if rows:
        return html, rows
    if total_records == 0:
        return html, rows

    for attempt in range(1, retries_on_empty + 1):
        time.sleep(1.0 * attempt + random.uniform(0.2, 1.0))
        html = fetch_page_html(
            session=session,
            tru_code=tru.code,
            page=page,
            year=year,
            status=status,
            amount_from=amount_from,
            count_per_page=count_per_page,
            month=month,
        )
        rows = parse_lot_rows_from_html(html, tru)
        if rows:
            return html, rows
    return html, rows


def fetch_lots_for_filter(
    session: requests.Session,
    tru: TruCode,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    month: int | None,
    max_pages: int | None = None,
    retries_on_empty: int = 3,
) -> tuple[int, List[LotRow]]:
    html, rows = fetch_and_parse_page_rows(
        session=session,
        tru=tru,
        page=1,
        year=year,
        status=status,
        amount_from=amount_from,
        count_per_page=count_per_page,
        month=month,
        retries_on_empty=retries_on_empty,
    )
    total_records = parse_total_records(html)
    if total_records <= count_per_page:
        return total_records, rows

    total_pages = math.ceil(total_records / count_per_page)
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    for page in range(2, total_pages + 1):
        time.sleep(0.15)
        _, page_rows = fetch_and_parse_page_rows(
            session=session,
            tru=tru,
            page=page,
            year=year,
            status=status,
            amount_from=amount_from,
            count_per_page=count_per_page,
            month=month,
            retries_on_empty=retries_on_empty,
        )
        rows.extend(page_rows)
    return total_records, rows


def fetch_all_lots_for_tru(
    session: requests.Session,
    tru: TruCode,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    max_pages: int | None = None,
    retries_on_empty: int = 3,
    split_capped_by_month: bool = True,
    cap_split_threshold: int = CAP_SPLIT_THRESHOLD_DEFAULT,
) -> List[LotRow]:
    total_records, rows = fetch_lots_for_filter(
        session=session,
        tru=tru,
        year=year,
        status=status,
        amount_from=amount_from,
        count_per_page=count_per_page,
        month=None,
        max_pages=max_pages,
        retries_on_empty=retries_on_empty,
    )

    if not split_capped_by_month or total_records < cap_split_threshold:
        return rows

    print(
        f"[INFO] {tru.code}: total={total_records}, split by month",
        flush=True,
    )
    merged_rows: List[LotRow] = []
    seen_lots = set()
    for month in range(1, 13):
        _, month_rows = fetch_lots_for_filter(
            session=session,
            tru=tru,
            year=year,
            status=status,
            amount_from=amount_from,
            count_per_page=count_per_page,
            month=month,
            max_pages=max_pages,
            retries_on_empty=retries_on_empty,
        )
        for row in month_rows:
            key = (row.lot_number, row.tru_code)
            if key in seen_lots:
                continue
            seen_lots.add(key)
            merged_rows.append(row)
    return merged_rows or rows


def collect_lots_for_tru(
    tru: TruCode,
    year: str,
    status: str,
    amount_from: str,
    count_per_page: int,
    max_pages: int | None,
    retries_on_empty: int,
    split_capped_by_month: bool,
    cap_split_threshold: int,
) -> List[LotRow]:
    # requests.Session is not thread-safe; keep one session per worker task.
    session = create_session()
    try:
        return fetch_all_lots_for_tru(
            session=session,
            tru=tru,
            year=year,
            status=status,
            amount_from=amount_from,
            count_per_page=count_per_page,
            max_pages=max_pages,
            retries_on_empty=retries_on_empty,
            split_capped_by_month=split_capped_by_month,
            cap_split_threshold=cap_split_threshold,
        )
    finally:
        session.close()


def write_csv(path: str, rows: Iterable[LotRow]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_HEADERS)
        for row in rows:
            writer.writerow(row.as_csv_row())


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect lots by TRU codes for 2025.")
    parser.add_argument(
        "--source-sheet-id",
        default=SOURCE_SHEET_ID,
        help="Google Sheet ID with TRU codes.",
    )
    parser.add_argument(
        "--output-csv",
        default="lots_2025_by_tru.csv",
        help="Output CSV path.",
    )
    parser.add_argument("--year", default=DEFAULT_FILTERS["year"], help="Filter year.")
    parser.add_argument(
        "--status",
        default=DEFAULT_FILTERS["status"],
        help="Lot status code (360 = Закупка состоялась).",
    )
    parser.add_argument(
        "--amount-from",
        default=DEFAULT_FILTERS["amount_from"],
        help="Lower bound for lot amount.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel workers by TRU code.",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Only process first N TRU codes (0 = all).",
    )
    parser.add_argument(
        "--only-code",
        action="append",
        default=[],
        help="Process only specific TRU code(s). Can be repeated.",
    )
    parser.add_argument(
        "--only-name",
        default="",
        help="Product name to use with --only-code (optional).",
    )
    parser.add_argument(
        "--max-pages-per-code",
        type=int,
        default=0,
        help="Limit pages per code (0 = no limit).",
    )
    parser.add_argument(
        "--count-record",
        type=int,
        default=COUNT_PER_PAGE_DEFAULT,
        help="Rows per page requested from portal (max practical: 500).",
    )
    parser.add_argument(
        "--retries-on-empty",
        type=int,
        default=5,
        help="Extra retries only when page expected to have data.",
    )
    parser.add_argument(
        "--cap-split-threshold",
        type=int,
        default=CAP_SPLIT_THRESHOLD_DEFAULT,
        help="If total rows reach this threshold, split by month.",
    )
    parser.add_argument(
        "--split-capped-by-month",
        dest="split_capped_by_month",
        action="store_true",
        default=True,
        help="Split capped codes by month to avoid portal cap.",
    )
    parser.add_argument(
        "--no-split-capped-by-month",
        dest="split_capped_by_month",
        action="store_false",
        help="Disable month split for capped codes.",
    )
    parser.add_argument(
        "--save-progress-every",
        type=int,
        default=25,
        help="Rewrite output CSV every N processed codes (0 = only at end).",
    )
    args = parser.parse_args()

    session = create_session()

    tru_codes = fetch_tru_codes(session, args.source_sheet_id)
    session.close()
    if args.only_code:
        explicit_name = normalize_ws(args.only_name)
        code_to_name = {item.code: item.name for item in tru_codes}
        selected_codes = [normalize_ws(code) for code in args.only_code if normalize_ws(code)]
        tru_codes = [
            TruCode(code=code, name=explicit_name or code_to_name.get(code, ""))
            for code in selected_codes
        ]
    if args.limit_codes and args.limit_codes > 0:
        tru_codes = tru_codes[: args.limit_codes]
    if not tru_codes:
        print("No TRU codes found.", file=sys.stderr)
        return 1

    max_pages = args.max_pages_per_code if args.max_pages_per_code > 0 else None
    all_rows: List[LotRow] = []
    seen_keys = set()

    start = time.time()
    print(f"Processing TRU codes: {len(tru_codes)}", flush=True)
    if args.save_progress_every > 0:
        write_csv(args.output_csv, all_rows)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {
            pool.submit(
                collect_lots_for_tru,
                tru,
                args.year,
                args.status,
                args.amount_from,
                max(1, args.count_record),
                max_pages,
                max(0, args.retries_on_empty),
                args.split_capped_by_month,
                max(0, args.cap_split_threshold),
            ): tru
            for tru in tru_codes
        }

        processed = 0
        for fut in as_completed(future_map):
            tru = future_map[fut]
            processed += 1
            try:
                rows = fut.result()
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[WARN] {tru.code}: failed ({exc})", flush=True)
                continue

            for row in rows:
                key = (row.lot_number, row.tru_code)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_rows.append(row)

            if processed % 25 == 0 or processed == len(tru_codes):
                print(
                    f"Processed {processed}/{len(tru_codes)} codes; "
                    f"rows collected: {len(all_rows)}",
                    flush=True,
                )
            if args.save_progress_every > 0 and (
                processed % args.save_progress_every == 0
                or processed == len(tru_codes)
            ):
                all_rows.sort(key=lambda r: (r.tru_code, r.lot_number))
                write_csv(args.output_csv, all_rows)

    all_rows.sort(key=lambda r: (r.tru_code, r.lot_number))
    write_csv(args.output_csv, all_rows)

    elapsed = time.time() - start
    print(
        f"Done. Rows: {len(all_rows)}. Output: {args.output_csv}. "
        f"Elapsed: {elapsed:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
