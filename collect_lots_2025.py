#!/usr/bin/env python3
"""Collect 2025 procurement lots by TRU codes from public sources."""

from __future__ import annotations

import argparse
import csv
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import local
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"
SOURCE_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/export?format=csv&gid=0"


thread_data = local()


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def get_session() -> requests.Session:
    if not hasattr(thread_data, "session"):
        thread_data.session = build_session()
    return thread_data.session


def read_source_codes() -> list[TruCode]:
    session = build_session()
    response = session.get(SOURCE_CSV_URL, timeout=60)
    response.raise_for_status()
    # Public export can come without charset header; enforce UTF-8.
    text = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    result: list[TruCode] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        code = row[0].strip()
        name = row[1].strip()
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    return result


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for link in soup.select("ul.pagination a[href]"):
        href = link.get("href", "")
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if "page" in query:
            try:
                max_page = max(max_page, int(query["page"][0]))
            except (TypeError, ValueError, IndexError):
                continue
        else:
            # Fallback for malformed links where query parser misses the value.
            match = re.search(r"[?&]page=(\\d+)", href)
            if match:
                max_page = max(max_page, int(match.group(1)))
    return max_page


def clean_cell_text(element) -> str:
    text = " ".join(element.stripped_strings)
    return re.sub(r"\\s+", " ", text).strip()


def get_lots_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [clean_cell_text(th) for th in table.select("thead th")]
        if not headers:
            continue
        if "№ лота" in headers and "Сумма, тг." in headers:
            return table
    return None


def extract_rows_from_table(table) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        lot_num = clean_cell_text(tds[0])
        if not lot_num or "-" not in lot_num:
            continue

        ann_name = ""
        ann_strong = tds[1].find("strong")
        if ann_strong is not None:
            ann_name = clean_cell_text(ann_strong)
        if not ann_name:
            ann_name = clean_cell_text(tds[1])

        lot_desc = clean_cell_text(tds[2]).replace("История", "").strip()
        qty = clean_cell_text(tds[3])
        amount = clean_cell_text(tds[4])
        method = clean_cell_text(tds[5])
        status = clean_cell_text(tds[6])

        rows.append(
            {
                "№ лота": lot_num,
                "Наименование объявления": ann_name,
                "Наименование и описание лота": lot_desc,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": method,
                "Статус": status,
            }
        )
    return rows


def fetch_code_lots(
    code: TruCode,
    year: int,
    status_code: str | None,
    amount_from: str | None,
    count_record: int,
    timeout: int,
) -> list[dict[str, str]]:
    session = get_session()
    base_params = {
        "filter[enstru]": code.code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "smb": "",
    }
    if status_code:
        base_params["filter[status][]"] = status_code
    if amount_from:
        base_params["filter[amount_from]"] = amount_from

    collected: list[dict[str, str]] = []
    page = 1
    max_page = 1
    while page <= max_page:
        params = dict(base_params)
        params["page"] = str(page)

        # Manual retries for abrupt connection resets.
        html = None
        for attempt in range(1, 6):
            try:
                response = session.get(LOTS_URL, params=params, timeout=timeout)
                response.raise_for_status()
                html = response.text
                break
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(min(5.0, attempt * 0.7))
        if html is None:
            break

        soup = BeautifulSoup(html, "lxml")
        table = get_lots_table(soup)
        if table is None:
            break
        page_rows = extract_rows_from_table(table)
        for row in page_rows:
            row["Код ТРУ"] = code.code
            row["Наименование товара"] = code.name
        collected.extend(page_rows)

        if page == 1:
            max_page = parse_max_page(soup)
        page += 1
    return collected


def chunks(items: list[TruCode], size: int) -> Iterable[list[TruCode]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    header = [
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
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU code.")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status-code", default="360", help="goszakup status code filter")
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="minimal lot amount, blank to disable",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0, help="for testing only")
    parser.add_argument("--output", default="/workspace/lots_2025_by_tru.csv")
    parser.add_argument("--progress-output", default="/workspace/lots_2025_progress.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    amount_from = args.amount_from.strip() or None
    status_code = args.status_code.strip() or None

    codes = read_source_codes()
    if args.limit > 0:
        codes = codes[: args.limit]
    total_codes = len(codes)
    print(f"Loaded {total_codes} TRU codes from source sheet.")

    all_rows: list[dict[str, str]] = []
    processed = 0
    started = time.time()

    for batch in chunks(codes, args.batch_size):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map = {
                pool.submit(
                    fetch_code_lots,
                    code=code,
                    year=args.year,
                    status_code=status_code,
                    amount_from=amount_from,
                    count_record=args.count_record,
                    timeout=args.timeout,
                ): code
                for code in batch
            }
            for future in as_completed(future_map):
                code = future_map[future]
                processed += 1
                try:
                    rows = future.result()
                    all_rows.extend(rows)
                    print(
                        f"[{processed}/{total_codes}] {code.code}: "
                        f"{len(rows)} rows (total {len(all_rows)})"
                    )
                except Exception as error:
                    print(f"[{processed}/{total_codes}] {code.code}: ERROR {error}")

        # Save progress after each batch in case long run is interrupted.
        write_csv(args.progress_output, all_rows)
        elapsed = time.time() - started
        print(
            f"Batch completed. Processed {processed}/{total_codes} codes, "
            f"rows={len(all_rows)}, elapsed={elapsed:.1f}s"
        )

    all_rows.sort(key=lambda row: (row["Код ТРУ"], row["№ лота"]))
    write_csv(args.output, all_rows)
    print(f"Done. Final rows: {len(all_rows)}. Saved to: {args.output}")
    print(f"Progress file: {args.progress_output}")
    print(f"Target sheet (manual import): https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/edit")


if __name__ == "__main__":
    main()
