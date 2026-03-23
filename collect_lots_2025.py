#!/usr/bin/env python3
"""Collect 2025 lots from goszakup by TRU codes from a Google Sheet."""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

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


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


def build_source_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_int_from_spaced(value: str) -> int:
    return int(re.sub(r"[^\d]", "", value))


def download_text(session: requests.Session, url: str, *, params: dict | None = None) -> str:
    for attempt in range(1, 6):
        try:
            response = session.get(url, params=params, timeout=45)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # pylint: disable=broad-except
            if attempt == 5:
                raise RuntimeError(f"Failed to GET {url} params={params}") from exc
            time.sleep(1.5 * attempt)
    raise RuntimeError("Unreachable retry state")


def load_tru_codes(session: requests.Session, sheet_id: str) -> list[TruCode]:
    csv_url = build_source_csv_url(sheet_id)
    content = download_text(session, csv_url)
    reader = csv.DictReader(io.StringIO(content))
    result: list[TruCode] = []
    for row in reader:
        code = clean_text(row.get("Код ТРУ", ""))
        name = clean_text(row.get("Название", ""))
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    if not result:
        raise RuntimeError("No TRU codes loaded from source sheet")
    return result


def detect_lots_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_text = clean_text(header_row.get_text(" ", strip=True))
        if "№ лота" in header_text and "Наименование объявления" in header_text:
            return table
    return None


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.select("ul.pagination a[href]"):
        href = a.get("href", "")
        if "page=" not in href:
            continue
        query = parse_qs(urlparse(href).query)
        page_values = query.get("page", [])
        if not page_values:
            continue
        try:
            max_page = max(max_page, int(page_values[0]))
        except ValueError:
            continue
    return max_page


def parse_total_records(soup: BeautifulSoup) -> int | None:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей", text)
    if not match:
        return None
    return parse_int_from_spaced(match.group(1))


def parse_rows_from_table(table, tru_code: TruCode) -> list[dict[str, str]]:
    body = table.find("tbody")
    if not body:
        return []

    rows: list[dict[str, str]] = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        announcement_link = tds[1].find("a")
        lot_desc_link = tds[2].find("a")

        lot_number = clean_text(tds[0].get_text(" ", strip=True))
        announcement_name = clean_text(
            announcement_link.get_text(" ", strip=True)
            if announcement_link
            else tds[1].get_text(" ", strip=True)
        )
        lot_name = clean_text(
            lot_desc_link.get_text(" ", strip=True)
            if lot_desc_link
            else tds[2].get_text(" ", strip=True)
        )

        rows.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": tru_code.code,
                "Наименование товара": tru_code.name,
                "Наименование объявления": announcement_name,
                "Наименование и описание лота": lot_name,
                "Кол-во": clean_text(tds[3].get_text(" ", strip=True)),
                "Сумма, тг.": clean_text(tds[4].get_text(" ", strip=True)),
                "Способ закупки": clean_text(tds[5].get_text(" ", strip=True)),
                "Статус": clean_text(tds[6].get_text(" ", strip=True)),
            }
        )
    return rows


def fetch_lots_for_code(
    session: requests.Session,
    tru_code: TruCode,
    *,
    year: int,
    status: int | None,
    amount_from: int | None,
) -> list[dict[str, str]]:
    base_params: dict[str, str] = {
        "filter[enstru]": tru_code.code,
        "filter[year]": str(year),
        "count_record": "2000",
        "smb": "",
    }
    if status is not None:
        base_params["filter[status][]"] = str(status)
    if amount_from is not None:
        base_params["filter[amount_from]"] = str(amount_from)

    html = download_text(session, GOSZAKUP_SEARCH_URL, params=base_params)
    soup = BeautifulSoup(html, "lxml")
    table = detect_lots_table(soup)
    if table is None:
        return []

    total_records = parse_total_records(soup)
    max_page = parse_max_page(soup)
    if total_records is not None:
        max_page = max(max_page, math.ceil(total_records / 2000))

    all_rows = parse_rows_from_table(table, tru_code)

    for page in range(2, max_page + 1):
        params = dict(base_params)
        params["page"] = str(page)
        page_html = download_text(session, GOSZAKUP_SEARCH_URL, params=params)
        page_soup = BeautifulSoup(page_html, "lxml")
        page_table = detect_lots_table(page_soup)
        if page_table is None:
            continue
        all_rows.extend(parse_rows_from_table(page_table, tru_code))

    return all_rows


def chunks(items: list[TruCode], size: int) -> Iterable[list[TruCode]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def collect_lots(
    tru_codes: list[TruCode],
    *,
    year: int,
    status: int | None,
    amount_from: int | None,
    workers: int,
) -> list[dict[str, str]]:
    all_rows: list[dict[str, str]] = []
    completed = 0
    started_at = time.time()

    # Split by chunks to reduce chance of long-lived stale sessions.
    for group in chunks(tru_codes, 200):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {}
            for tru_code in group:
                session = requests.Session()
                session.headers.update(HEADERS)
                future = executor.submit(
                    fetch_lots_for_code,
                    session,
                    tru_code,
                    year=year,
                    status=status,
                    amount_from=amount_from,
                )
                future_map[future] = tru_code

            for future in as_completed(future_map):
                tru_code = future_map[future]
                completed += 1
                try:
                    rows = future.result()
                    all_rows.extend(rows)
                    print(
                        f"[{completed}/{len(tru_codes)}] {tru_code.code}: {len(rows)} rows",
                        flush=True,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    print(
                        f"[{completed}/{len(tru_codes)}] {tru_code.code}: ERROR {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

    elapsed = time.time() - started_at
    print(f"Collected rows: {len(all_rows)} in {elapsed:.1f}s", flush=True)
    return all_rows


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect 2025 lots by TRU codes from goszakup and output CSV."
    )
    parser.add_argument(
        "--source-sheet-id",
        default=SOURCE_SHEET_ID_DEFAULT,
        help="Google Sheet ID with columns: Код ТРУ, Название",
    )
    parser.add_argument("--year", type=int, default=2025, help="Financial year filter")
    parser.add_argument(
        "--status",
        type=int,
        default=360,
        help="Lot status filter id (360 = Закупка состоялась). Use -1 for no status filter.",
    )
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15_000_000,
        help="Minimum amount filter. Use -1 for no amount filter.",
    )
    parser.add_argument("--workers", type=int, default=8, help="Concurrent workers")
    parser.add_argument("--max-codes", type=int, default=0, help="Limit source codes for test run")
    parser.add_argument(
        "--output",
        default="output/lots_2025_by_tru.csv",
        help="Output CSV file path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = None if args.status == -1 else args.status
    amount_from = None if args.amount_from == -1 else args.amount_from

    session = requests.Session()
    session.headers.update(HEADERS)
    tru_codes = load_tru_codes(session, args.source_sheet_id)
    if args.max_codes > 0:
        tru_codes = tru_codes[: args.max_codes]

    print(f"Loaded TRU codes: {len(tru_codes)}", flush=True)
    rows = collect_lots(
        tru_codes,
        year=args.year,
        status=status,
        amount_from=amount_from,
        workers=max(1, args.workers),
    )
    rows.sort(key=lambda item: (item["Код ТРУ"], item["№ лота"]))

    output_path = Path(args.output)
    write_csv(rows, output_path)
    print(f"Saved CSV: {output_path.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
