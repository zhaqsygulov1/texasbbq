#!/usr/bin/env python3
"""
Collect 2025 lots from goszakup.gov.kz for TRU codes from a Google Sheet.

Input Google Sheet columns:
  - Код ТРУ
  - Название

Output CSV columns:
  - № лота
  - Код ТРУ
  - Наименование товара
  - Наименование объявления
  - Наименование и описание лота
  - Кол-во
  - Сумма, тг.
  - Способ закупки
  - Статус
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_GID = "0"
GOSZAKUP_LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"


thread_local = threading.local()


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


@dataclass(frozen=True)
class LotRow:
    lot_number: str
    tru_code: str
    product_name: str
    announce_name: str
    lot_name_desc: str
    quantity: str
    amount: str
    purchase_method: str
    status: str


def get_session() -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru,en;q=0.8",
            }
        )
        thread_local.session = session
    return session


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def build_source_csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def load_tru_codes(sheet_id: str, gid: str, timeout: int) -> List[TruCode]:
    url = build_source_csv_url(sheet_id, gid)
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    # Google CSV may include BOM.
    content = response.content.decode("utf-8-sig", errors="replace")
    rows = csv.DictReader(content.splitlines())

    result: List[TruCode] = []
    for row in rows:
        code = clean_text(row.get("Код ТРУ", ""))
        name = clean_text(row.get("Название", ""))
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    return result


def parse_total_records(html: str) -> int:
    match = re.search(r"Показано c\s+\d+\s+по\s+\d+\s+из\s+([\d\s]+)\s+записей", html)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_lot_rows(html: str, tru: TruCode) -> List[LotRow]:
    # The source markup often has unclosed <td> tags; lxml normalizes it better.
    soup = BeautifulSoup(html, "lxml")

    # The page contains multiple tables; target one has stable id.
    target_table = soup.find("table", id="search-result")
    if target_table is None:
        for table in soup.find_all("table"):
            headers = [clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
            if headers and "№ лота" in headers:
                target_table = table
                break

    if target_table is None:
        return []

    rows: List[LotRow] = []
    body = target_table.find("tbody")
    if body is None:
        return []

    for tr in body.find_all("tr"):
        # Markup is not perfectly valid HTML, so we avoid recursive=False here.
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = clean_text(tds[0].get_text(" ", strip=True))

        announce_link = tds[1].find("a")
        if announce_link:
            announce_name = clean_text(announce_link.get_text(" ", strip=True))
        else:
            announce_name = clean_text(tds[1].get_text(" ", strip=True))

        lot_link = tds[2].find("a", href=re.compile(r"/subpriceoffer/index/"))
        if lot_link:
            lot_name_desc = clean_text(lot_link.get_text(" ", strip=True))
        else:
            # Fallback if markup differs: extract first line from cell text.
            raw_lot_text = clean_text(tds[2].get_text(" ", strip=True))
            lot_name_desc = clean_text(raw_lot_text.replace("История", ""))
        quantity = clean_text(tds[3].get_text(" ", strip=True))
        amount = clean_text(tds[4].get_text(" ", strip=True))
        purchase_method = clean_text(tds[5].get_text(" ", strip=True))
        status = clean_text(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        rows.append(
            LotRow(
                lot_number=lot_number,
                tru_code=tru.code,
                product_name=tru.name,
                announce_name=announce_name,
                lot_name_desc=lot_name_desc,
                quantity=quantity,
                amount=amount,
                purchase_method=purchase_method,
                status=status,
            )
        )

    return rows


def build_query_params(
    tru_code: str,
    year: int,
    amount_from: str,
    count_record: int,
    page: int | None = None,
) -> List[Tuple[str, str]]:
    params: List[Tuple[str, str]] = [
        ("filter[name]", ""),
        ("filter[number]", ""),
        ("filter[number_anno]", ""),
        ("filter[enstru]", tru_code),
        ("filter[status][]", "360"),  # Закупка состоялась
        ("filter[customer]", ""),
        ("filter[amount_from]", amount_from),
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
        ("count_record", str(count_record)),
    ]
    if page and page > 1:
        params.append(("page", str(page)))
    return params


def fetch_html(params: Sequence[Tuple[str, str]], timeout: int, retries: int = 5) -> str:
    session = get_session()
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(GOSZAKUP_LOTS_URL, params=params, timeout=timeout)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code} from goszakup")
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == retries:
                break
            sleep_s = (2 ** (attempt - 1)) + random.random() * 0.5
            time.sleep(sleep_s)

    assert last_error is not None
    raise last_error


def collect_for_code(
    tru: TruCode,
    year: int,
    amount_from: str,
    count_record: int,
    timeout: int,
    request_delay: float,
) -> List[LotRow]:
    rows: List[LotRow] = []

    first_params = build_query_params(
        tru_code=tru.code,
        year=year,
        amount_from=amount_from,
        count_record=count_record,
    )
    first_html = fetch_html(first_params, timeout=timeout)
    rows.extend(parse_lot_rows(first_html, tru))

    total = parse_total_records(first_html)
    if total <= count_record:
        if request_delay:
            time.sleep(request_delay)
        return rows

    total_pages = math.ceil(total / count_record)
    for page in range(2, total_pages + 1):
        params = build_query_params(
            tru_code=tru.code,
            year=year,
            amount_from=amount_from,
            count_record=count_record,
            page=page,
        )
        html = fetch_html(params, timeout=timeout)
        rows.extend(parse_lot_rows(html, tru))
        if request_delay:
            time.sleep(request_delay)

    return rows


def deduplicate_rows(rows: Iterable[LotRow]) -> List[LotRow]:
    seen: set[Tuple[str, str]] = set()
    unique: List[LotRow] = []
    for row in rows:
        key = (row.lot_number, row.tru_code)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write_csv(rows: Sequence[LotRow], output_path: str) -> None:
    headers = [
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
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(
                [
                    row.lot_number,
                    row.tru_code,
                    row.product_name,
                    row.announce_name,
                    row.lot_name_desc,
                    row.quantity,
                    row.amount,
                    row.purchase_method,
                    row.status,
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU codes from Google Sheet.")
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--source-gid", default=SOURCE_GID)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--amount-from", default="15000000", help="Minimum amount filter as in goszakup search")
    parser.add_argument("--count-record", type=int, default=2000, help="goszakup page size")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=40, help="HTTP timeout seconds")
    parser.add_argument("--request-delay", type=float, default=0.0, help="Delay between page requests per code")
    parser.add_argument("--output", default="lots_2025_by_tru.csv")
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="For testing: process only first N TRU codes (0 = all)",
    )
    parser.add_argument("--single-code", default="", help="Process only this TRU code (for validation)")
    parser.add_argument("--single-name", default="", help="Product name for --single-code")
    args = parser.parse_args()

    print("Loading TRU codes from source sheet...", flush=True)
    if args.single_code:
        tru_codes = [TruCode(code=clean_text(args.single_code), name=clean_text(args.single_name))]
        print("Using single TRU code from CLI.", flush=True)
    else:
        tru_codes = load_tru_codes(args.source_sheet_id, args.source_gid, timeout=args.timeout)
        if args.limit_codes and args.limit_codes > 0:
            tru_codes = tru_codes[: args.limit_codes]
            print(f"Code list limited to first {len(tru_codes)} rows.", flush=True)

    if not tru_codes:
        print("No TRU codes found in source sheet.", file=sys.stderr)
        return 2
    print(f"Loaded {len(tru_codes)} TRU codes.", flush=True)

    collected: List[LotRow] = []
    failed: List[Tuple[str, str]] = []
    lock = threading.Lock()
    done_counter = 0

    def worker(tru: TruCode) -> List[LotRow]:
        return collect_for_code(
            tru=tru,
            year=args.year,
            amount_from=args.amount_from,
            count_record=args.count_record,
            timeout=args.timeout,
            request_delay=args.request_delay,
        )

    print(
        "Collecting lots from goszakup "
        f"(year={args.year}, min_amount={args.amount_from}, workers={args.workers})...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(worker, tru): tru for tru in tru_codes}
        for future in as_completed(future_map):
            tru = future_map[future]
            try:
                result = future.result()
                with lock:
                    collected.extend(result)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    failed.append((tru.code, str(exc)))
            finally:
                with lock:
                    done_counter += 1
                    if done_counter % args.progress_every == 0 or done_counter == len(tru_codes):
                        print(f"Progress: {done_counter}/{len(tru_codes)} codes processed.", flush=True)

    unique_rows = deduplicate_rows(collected)
    write_csv(unique_rows, args.output)

    print(f"Collected rows (raw): {len(collected)}", flush=True)
    print(f"Collected rows (unique): {len(unique_rows)}", flush=True)
    print(f"Failed codes: {len(failed)}", flush=True)
    if failed:
        failed_path = args.output.rsplit(".", 1)[0] + "_failed_codes.csv"
        with open(failed_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Код ТРУ", "Ошибка"])
            writer.writerows(failed)
        print(f"Failed code list saved to: {failed_path}", flush=True)

    print(f"Output file: {args.output}", flush=True)
    print(
        "Done. Note: automatic write to Google Sheets is not included in this script "
        "because it requires authenticated Google API credentials.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
