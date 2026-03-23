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


def normalize_tru_code(raw_code: str) -> str:
    """Normalize TRU code to 6.3.6 format where possible."""
    value = clean_text(raw_code)
    if not value:
        return value

    parts = value.split(".")
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        middle = parts[1][:3].ljust(3, "0")
        return f"{parts[0].zfill(6)}.{middle}.000000"
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        middle = parts[1][:3].ljust(3, "0")
        suffix = parts[2][:6].ljust(6, "0")
        return f"{parts[0].zfill(6)}.{middle}.{suffix}"
    return value


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
    for attempt in range(1, 6):
        try:
            response = session.get(csv_url, timeout=45)
            response.raise_for_status()
            content = response.content.decode("utf-8-sig")
            break
        except Exception as exc:  # pylint: disable=broad-except
            if attempt == 5:
                raise RuntimeError(f"Failed to load source sheet CSV: {csv_url}") from exc
            time.sleep(1.5 * attempt)

    stream = io.StringIO(content)
    reader = csv.reader(stream)
    try:
        raw_headers = next(reader)
    except StopIteration as exc:
        raise RuntimeError("Source sheet CSV is empty") from exc

    headers = [clean_text(h.lstrip("\ufeff")) for h in raw_headers]
    try:
        code_index = headers.index("Код ТРУ")
        name_index = headers.index("Название")
    except ValueError as exc:
        raise RuntimeError(f"Unexpected source sheet headers: {headers}") from exc

    result: list[TruCode] = []
    converted = 0
    seen_codes: set[str] = set()
    for row in reader:
        if code_index >= len(row):
            continue
        code = normalize_tru_code(row[code_index])
        name = clean_text(row[name_index]) if name_index < len(row) else ""
        if not code:
            continue
        if code != clean_text(row[code_index]):
            converted += 1
        if code in seen_codes:
            continue
        seen_codes.add(code)
        result.append(TruCode(code=code, name=name))
    if not result:
        raise RuntimeError("No TRU codes loaded from source sheet")
    if converted:
        print(f"Normalized TRU codes: {converted}", flush=True)
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


def collect_lots_to_csv(
    tru_codes: list[TruCode],
    *,
    year: int,
    status: int | None,
    amount_from: int | None,
    workers: int,
    output_path: Path,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = 0
    total_rows = 0
    started_at = time.time()

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

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
                        if rows:
                            writer.writerows(rows)
                            f.flush()
                        total_rows += len(rows)
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
    print(f"Collected rows: {total_rows} in {elapsed:.1f}s", flush=True)
    return total_rows


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
    output_path = Path(args.output)
    total_rows = collect_lots_to_csv(
        tru_codes,
        year=args.year,
        status=status,
        amount_from=amount_from,
        workers=max(1, args.workers),
        output_path=output_path,
    )
    print(f"Total rows written: {total_rows}", flush=True)
    print(f"Saved CSV: {output_path.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
