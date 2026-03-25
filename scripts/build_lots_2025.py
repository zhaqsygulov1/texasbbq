#!/usr/bin/env python3
import argparse
import csv
import io
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
FULL_TRU_CODE_RE = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")

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


@dataclass
class TruItem:
    code: str
    name: str


@dataclass
class LotRow:
    lot_no: str
    tru_code: str
    product_name: str
    announce_name: str
    lot_name_desc: str
    quantity: str
    amount: str
    method: str
    status: str

    def as_csv_row(self) -> List[str]:
        return [
            self.lot_no,
            self.tru_code,
            self.product_name,
            self.announce_name,
            self.lot_name_desc,
            self.quantity,
            self.amount,
            self.method,
            self.status,
        ]


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def build_source_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid=0"


def fetch_with_retry(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, str]] = None,
    timeout: int = 45,
    retries: int = 5,
    backoff: float = 1.0,
) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt == retries:
                break
            sleep_for = backoff * (2 ** (attempt - 1)) + random.uniform(0.05, 0.35)
            time.sleep(sleep_for)
    raise RuntimeError(f"Request failed after {retries} attempts: {url}") from last_error


def read_tru_codes(
    session: requests.Session,
    sheet_id: str,
    strict_code_format: bool = True,
) -> Tuple[List[TruItem], List[TruItem]]:
    response = fetch_with_retry(session, build_source_csv_url(sheet_id))
    text = response.content.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []

    items: List[TruItem] = []
    skipped: List[TruItem] = []
    for row in rows[1:]:
        if not row:
            continue
        code = clean_text(row[0] if len(row) > 0 else "")
        name = clean_text(row[1] if len(row) > 1 else "")
        if not code:
            continue
        item = TruItem(code=code, name=name)
        if strict_code_format and not FULL_TRU_CODE_RE.match(code):
            skipped.append(item)
            continue
        items.append(item)
    return items, skipped


def extract_total_rows(soup: BeautifulSoup) -> int:
    info_text = ""
    for s in soup.stripped_strings:
        if "Показано c" in s and "из" in s:
            info_text = s
            break
    if not info_text:
        return 0
    match = re.search(r"из\s+([\d\s]+)\s+записей", info_text)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_row_to_lot(tds: List, tru_item: TruItem) -> LotRow:
    lot_no = clean_text(tds[0].get_text(" ", strip=True)) if len(tds) > 0 else ""

    announce_name = ""
    if len(tds) > 1:
        link = tds[1].find("a")
        if link:
            announce_name = clean_text(link.get_text(" ", strip=True))
        if not announce_name:
            announce_name = clean_text(tds[1].get_text(" ", strip=True))
            announce_name = announce_name.split("Заказчик:")[0].strip()

    lot_name_desc = ""
    if len(tds) > 2:
        link = tds[2].find("a")
        if link:
            lot_name_desc = clean_text(link.get_text(" ", strip=True))
        if not lot_name_desc:
            lot_name_desc = clean_text(tds[2].get_text(" ", strip=True))
            lot_name_desc = lot_name_desc.replace("История", "").strip()

    quantity = clean_text(tds[3].get_text(" ", strip=True)) if len(tds) > 3 else ""
    amount = clean_text(tds[4].get_text(" ", strip=True)) if len(tds) > 4 else ""
    method = clean_text(tds[5].get_text(" ", strip=True)) if len(tds) > 5 else ""
    status = clean_text(tds[6].get_text(" ", strip=True)) if len(tds) > 6 else ""

    return LotRow(
        lot_no=lot_no,
        tru_code=tru_item.code,
        product_name=tru_item.name,
        announce_name=announce_name,
        lot_name_desc=lot_name_desc,
        quantity=quantity,
        amount=amount,
        method=method,
        status=status,
    )


def fetch_lot_page(
    session: requests.Session,
    tru_item: TruItem,
    year: int,
    page: int,
    count_record: int,
    status: Optional[str],
    amount_from: Optional[str],
    amount_to: Optional[str],
) -> Tuple[List[LotRow], int]:
    params: Dict[str, str] = {
        "filter[enstru]": tru_item.code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
        "smb": "",
    }
    if status:
        params["filter[status][]"] = status
    if amount_from:
        params["filter[amount_from]"] = amount_from
    if amount_to:
        params["filter[amount_to]"] = amount_to

    response = fetch_with_retry(session, GOSZAKUP_SEARCH_URL, params=params)
    soup = BeautifulSoup(response.text, "lxml")

    total_rows = extract_total_rows(soup)
    parsed_rows: List[LotRow] = []
    for tr in soup.select("#search-result tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        row = parse_row_to_lot(tds, tru_item)
        if row.lot_no:
            parsed_rows.append(row)
    return parsed_rows, total_rows


def collect_for_tru(
    tru_item: TruItem,
    year: int,
    count_record: int,
    status: Optional[str],
    amount_from: Optional[str],
    amount_to: Optional[str],
    page_limit: Optional[int],
) -> List[LotRow]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    first_rows, total_rows = fetch_lot_page(
        session=session,
        tru_item=tru_item,
        year=year,
        page=1,
        count_record=count_record,
        status=status,
        amount_from=amount_from,
        amount_to=amount_to,
    )
    if total_rows == 0:
        return []

    total_pages = max(1, math.ceil(total_rows / count_record))
    if page_limit is not None:
        total_pages = min(total_pages, page_limit)

    all_rows = list(first_rows)
    for page in range(2, total_pages + 1):
        page_rows, _ = fetch_lot_page(
            session=session,
            tru_item=tru_item,
            year=year,
            page=page,
            count_record=count_record,
            status=status,
            amount_from=amount_from,
            amount_to=amount_to,
        )
        if not page_rows:
            break
        all_rows.extend(page_rows)

    # Unique by lot number + code to avoid accidental duplicates
    unique: Dict[Tuple[str, str], LotRow] = {}
    for row in all_rows:
        unique[(row.lot_no, row.tru_code)] = row
    return list(unique.values())


def write_csv(path: str, rows: Iterable[LotRow]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_HEADERS)
        for row in rows:
            writer.writerow(row.as_csv_row())


def write_skipped_codes(path: str, skipped: List[TruItem]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Код ТРУ", "Название", "Причина"])
        for item in skipped:
            writer.writerow([item.code, item.name, "Неполный формат кода"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build lots list for 2025 by TRU codes.")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--target-sheet-id", default=TARGET_SHEET_ID)
    parser.add_argument("--output", default="output/lots_2025_by_tru_codes.csv")
    parser.add_argument("--status", default="360", help="goszakup status code (default: 360)")
    parser.add_argument("--amount-from", default="15000000")
    parser.add_argument("--amount-to", default="")
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--page-limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--strict-code-format",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use only full ENSTRU codes like 000000.000.000000 (default: enabled).",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    tru_items, skipped_codes = read_tru_codes(
        session,
        args.source_sheet_id,
        strict_code_format=args.strict_code_format,
    )

    if args.max_codes > 0:
        tru_items = tru_items[: args.max_codes]

    print(f"Loaded TRU rows: {len(tru_items)}")
    if skipped_codes:
        print(f"Skipped non-full TRU codes: {len(skipped_codes)}")
    if not tru_items:
        print("No TRU codes found, exiting.")
        return 1

    amount_to = args.amount_to or None
    status = args.status or None
    page_limit = args.page_limit if args.page_limit > 0 else None

    all_rows: List[LotRow] = []
    processed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {
            pool.submit(
                collect_for_tru,
                tru_item=item,
                year=args.year,
                count_record=args.count_record,
                status=status,
                amount_from=args.amount_from,
                amount_to=amount_to,
                page_limit=page_limit,
            ): item
            for item in tru_items
        }
        for future in as_completed(future_map):
            item = future_map[future]
            processed += 1
            try:
                rows = future.result()
            except Exception as error:  # noqa: BLE001
                print(f"[{processed}/{len(tru_items)}] ERROR for {item.code}: {error}")
                continue
            if rows:
                all_rows.extend(rows)
                print(f"[{processed}/{len(tru_items)}] {item.code} -> {len(rows)} rows")
            elif processed % 100 == 0:
                print(f"[{processed}/{len(tru_items)}] processed...")

    # Global dedupe
    unique: Dict[Tuple[str, str], LotRow] = {}
    for row in all_rows:
        unique[(row.lot_no, row.tru_code)] = row
    final_rows = list(unique.values())
    final_rows.sort(key=lambda r: (r.tru_code, r.lot_no))

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    write_csv(args.output, final_rows)
    if skipped_codes:
        skipped_path = (
            f"{os.path.splitext(args.output)[0]}_skipped_codes.csv"
        )
        write_skipped_codes(skipped_path, skipped_codes)
        print(f"Skipped codes report: {skipped_path}")

    print(f"Done. Rows collected: {len(final_rows)}")
    print(f"Source sheet: https://docs.google.com/spreadsheets/d/{args.source_sheet_id}/edit")
    print(f"Target sheet: https://docs.google.com/spreadsheets/d/{args.target_sheet_id}/edit")
    print(f"Output file: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
