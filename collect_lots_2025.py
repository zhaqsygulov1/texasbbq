#!/usr/bin/env python3
import argparse
import csv
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import StringIO
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SOURCE_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/export?format=csv"
TARGET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/export?format=csv"
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"

OUTPUT_COLUMNS = [
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


@dataclass
class LotRow:
    lot_no: str
    code: str
    product_name: str
    announcement_name: str
    lot_name: str
    quantity: str
    amount_tenge: str
    purchase_method: str
    status: str

    def as_csv_row(self) -> List[str]:
        return [
            self.lot_no,
            self.code,
            self.product_name,
            self.announcement_name,
            self.lot_name,
            self.quantity,
            self.amount_tenge,
            self.purchase_method,
            self.status,
        ]


def build_session() -> requests.Session:
    session = requests.Session()
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


def get_with_retries(
    session: requests.Session,
    url: str,
    params: Optional[Iterable[Tuple[str, str]]] = None,
    max_attempts: int = 5,
    timeout: int = 40,
) -> requests.Response:
    delay = 1.0
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == max_attempts:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Failed to fetch URL after {max_attempts} attempts: {url}") from last_error


def normalize_space(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def load_tru_codes(session: requests.Session) -> List[Tuple[str, str]]:
    response = get_with_retries(session, SOURCE_CSV_URL)
    # Google Sheets CSV export is UTF-8, but requests may guess a different
    # encoding from headers. Decode bytes explicitly to avoid mojibake.
    try:
        decoded = response.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = response.content.decode("utf-8", errors="replace")

    reader = csv.reader(StringIO(decoded))
    rows = list(reader)
    if not rows:
        raise RuntimeError("Source table is empty.")

    unique: Dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        code = normalize_space(row[0]) if len(row) > 0 else ""
        name = normalize_space(row[1]) if len(row) > 1 else ""
        if not code:
            continue
        if code not in unique:
            unique[code] = name
    return list(unique.items())


def parse_total_and_page_size(page_text: str) -> Tuple[int, int]:
    match = re.search(r"Показано c\s+(\d+)\s+по\s+(\d+)\s+из\s+([\d\s]+)\s+записей", page_text)
    if not match:
        return 0, 40
    first = int(match.group(1))
    last = int(match.group(2))
    total = int(match.group(3).replace(" ", ""))
    page_size = max(1, last - first + 1)
    return total, page_size


def extract_text_or_first_anchor(td) -> str:  # type: ignore[no-untyped-def]
    anchor = td.find("a")
    if anchor:
        return normalize_space(anchor.get_text(" ", strip=True))
    return normalize_space(td.get_text(" ", strip=True))


def extract_lot_number(td) -> str:  # type: ignore[no-untyped-def]
    raw = normalize_space(td.get_text(" ", strip=True))
    if not raw:
        return ""
    return raw.split()[0]


def parse_lots_from_html(html: str, code: str, product_name: str) -> Tuple[List[LotRow], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    total_count, page_size = parse_total_and_page_size(page_text)

    lots_table = None
    for table in soup.find_all("table"):
        headers = [normalize_space(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if "№ лота" in headers and "Наименование объявления" in headers:
            lots_table = table
            break

    if not lots_table:
        return [], total_count, page_size

    parsed_rows: List[LotRow] = []
    for tr in lots_table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue

        lot_no = extract_lot_number(cells[0])
        announcement_name = extract_text_or_first_anchor(cells[1])
        lot_name = extract_text_or_first_anchor(cells[2])
        quantity = normalize_space(cells[3].get_text(" ", strip=True))
        amount_tenge = normalize_space(cells[4].get_text(" ", strip=True))
        purchase_method = normalize_space(cells[5].get_text(" ", strip=True))
        status = normalize_space(cells[6].get_text(" ", strip=True))

        if not lot_no:
            continue

        parsed_rows.append(
            LotRow(
                lot_no=lot_no,
                code=code,
                product_name=product_name,
                announcement_name=announcement_name,
                lot_name=lot_name,
                quantity=quantity,
                amount_tenge=amount_tenge,
                purchase_method=purchase_method,
                status=status,
            )
        )

    return parsed_rows, total_count, page_size


def build_search_params(
    code: str,
    year: int,
    amount_from: Optional[str],
    status_codes: List[str],
    page: int,
) -> List[Tuple[str, str]]:
    params: List[Tuple[str, str]] = [
        ("filter[name]", ""),
        ("filter[number]", ""),
        ("filter[number_anno]", ""),
        ("filter[enstru]", code),
        ("filter[customer]", ""),
        ("filter[amount_from]", amount_from or ""),
        ("filter[amount_to]", ""),
        ("filter[trade_type]", ""),
        ("filter[month]", ""),
        ("filter[plan_number]", ""),
        ("filter[end_date_from]", ""),
        ("filter[end_date_to]", ""),
        ("filter[start_date_to]", ""),
        ("filter[year]", str(year)),
        ("filter[itogi_date_from]", ""),
        ("filter[itogi_date_to]", ""),
        ("filter[start_date_from]", ""),
        ("filter[more]", ""),
        ("smb", ""),
        ("page", str(page)),
    ]
    for status_code in status_codes:
        params.append(("filter[status][]", status_code))
    return params


def collect_for_code(
    code: str,
    product_name: str,
    year: int,
    amount_from: Optional[str],
    status_codes: List[str],
    sleep_seconds: float,
) -> List[LotRow]:
    session = build_session()
    all_rows: List[LotRow] = []
    page = 1
    expected_pages: Optional[int] = None

    while True:
        params = build_search_params(
            code=code,
            year=year,
            amount_from=amount_from,
            status_codes=status_codes,
            page=page,
        )
        response = get_with_retries(session, BASE_URL, params=params)
        rows, total_count, page_size = parse_lots_from_html(response.text, code, product_name)

        if expected_pages is None:
            if total_count == 0:
                break
            expected_pages = max(1, math.ceil(total_count / page_size))

        if not rows:
            break

        all_rows.extend(rows)

        if expected_pages is not None and page >= expected_pages:
            break

        page += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return all_rows


def write_csv(rows: List[LotRow], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_COLUMNS)
        for row in rows:
            writer.writerow(row.as_csv_row())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect goszakup lots by TRU codes for 2025.")
    parser.add_argument(
        "--output",
        default="lots_2025_by_tru.csv",
        help="Output CSV file path.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="Financial year filter for lots.",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Minimum lot amount filter. Empty string disables the filter.",
    )
    parser.add_argument(
        "--status",
        action="append",
        default=None,
        help=(
            "Lot status code filter (repeatable). "
            "Default: 360 (Закупка состоялась). Use --status '' to disable."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel workers for TRU codes.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.08,
        help="Delay in seconds between paginated requests per code.",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="For testing: process only the first N TRU codes (0 means all).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    amount_from = args.amount_from.strip() if args.amount_from is not None else ""
    amount_from = amount_from if amount_from else None

    raw_statuses = args.status if args.status is not None else ["360"]
    status_codes = [s for s in raw_statuses if s]
    session = build_session()
    tru_codes = load_tru_codes(session)
    if args.limit_codes and args.limit_codes > 0:
        tru_codes = tru_codes[: args.limit_codes]

    print(f"Loaded TRU codes: {len(tru_codes)}")
    print(f"Target sheet (read-only check): {TARGET_CSV_URL}")
    print(
        "Filters -> "
        f"year={args.year}, amount_from={amount_from or 'none'}, "
        f"status={status_codes if status_codes else 'all'}"
    )

    all_rows: List[LotRow] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                collect_for_code,
                code,
                name,
                args.year,
                amount_from,
                status_codes,
                args.sleep,
            ): (code, name)
            for code, name in tru_codes
        }

        for future in as_completed(futures):
            code, _name = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[{completed}/{len(tru_codes)}] ERROR {code}: {exc}", file=sys.stderr)
                continue

            all_rows.extend(rows)
            print(
                f"[{completed}/{len(tru_codes)}] {code}: +{len(rows)} rows "
                f"(total={len(all_rows)})"
            )

    # Deduplicate in case of pagination overlap or retries.
    seen: set[Tuple[str, str]] = set()
    unique_rows: List[LotRow] = []
    for row in all_rows:
        key = (row.lot_no, row.code)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)

    write_csv(unique_rows, args.output)
    print(f"Done. Saved {len(unique_rows)} rows to: {args.output}")
    print(
        "Note: direct write to Google Sheet requires authenticated Google API access; "
        "this script prepares an import-ready CSV."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
