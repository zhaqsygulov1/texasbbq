#!/usr/bin/env python3
"""Collect 2025 lots from goszakup by TRU code list.

Input:
  - Google Sheet with columns "Код ТРУ" and "Название"

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
import io
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
GOSZAKUP_LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"
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
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def build_csv_export_url(sheet_id: str, gid: int = 0) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def fetch_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    timeout: int = 45,
    attempts: int = 6,
    base_sleep: float = 1.2,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as err:  # noqa: BLE001
            last_error = err
            if attempt == attempts:
                break
            pause = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(pause)
    if last_error is None:
        raise RuntimeError("Unknown network error")
    raise RuntimeError(f"Failed to fetch {url} after {attempts} attempts: {last_error}") from last_error


def read_tru_codes(sheet_id: str, gid: int = 0) -> list[TruCode]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    url = build_csv_export_url(sheet_id, gid=gid)
    response = fetch_with_retries(session, url, timeout=90, attempts=6)

    text = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise RuntimeError("TRU source sheet is empty")

    header = [normalize_ws(col) for col in rows[0]]
    if len(header) < 2:
        raise RuntimeError("TRU source sheet must contain at least 2 columns")

    # Expected format from the user: "Код ТРУ - Название".
    code_idx = 0
    name_idx = 1
    for idx, col in enumerate(header):
        low = col.lower()
        if "код" in low and "тру" in low:
            code_idx = idx
        if "назв" in low:
            name_idx = idx

    seen: set[str] = set()
    result: list[TruCode] = []
    for row in rows[1:]:
        if len(row) <= code_idx:
            continue
        code = normalize_ws(row[code_idx])
        if not code or code in seen:
            continue
        name = normalize_ws(row[name_idx]) if len(row) > name_idx else ""
        result.append(TruCode(code=code, name=name))
        seen.add(code)
    return result


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for anchor in soup.select("ul.pagination a[href]"):
        href = anchor.get("href", "")
        if not href:
            continue
        query = parse_qs(urlparse(href).query)
        page_values = query.get("page", [])
        if not page_values:
            continue
        try:
            page = int(page_values[0])
        except ValueError:
            continue
        max_page = max(max_page, page)
    return max_page


def parse_lot_rows(html: str, tru: TruCode) -> tuple[list[list[str]], int]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="search-result")
    if table is None:
        return [], 1

    tbody = table.find("tbody")
    if tbody is None:
        return [], 1

    rows: list[list[str]] = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue

        lot_number = normalize_ws(tds[0].get_text(" ", strip=True))
        if not lot_number:
            continue

        announce_link = tds[1].find("a")
        if announce_link is not None:
            announce = normalize_ws(announce_link.get_text(" ", strip=True))
        else:
            announce = normalize_ws(tds[1].get_text(" ", strip=True))

        lot_cell = BeautifulSoup(str(tds[2]), "lxml")
        for extra in lot_cell.select(".btn-select-history, script, style"):
            extra.decompose()
        lot_description = normalize_ws(lot_cell.get_text(" ", strip=True).replace("История", ""))

        quantity = normalize_ws(tds[3].get_text(" ", strip=True))
        amount = normalize_ws(tds[4].get_text(" ", strip=True))
        method = normalize_ws(tds[5].get_text(" ", strip=True))
        status = normalize_ws(tds[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_number,
                tru.code,
                tru.name,
                announce,
                lot_description,
                quantity,
                amount,
                method,
                status,
            ]
        )

    return rows, parse_max_page(soup)


def fetch_code_rows(
    tru: TruCode,
    *,
    year: int,
    count_per_page: int,
    max_pages_per_code: int,
    sleep_between_pages: float,
) -> list[list[str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    def get_page(page: int) -> tuple[list[list[str]], int]:
        params = {
            "filter[enstru]": tru.code,
            "filter[year]": str(year),
            "count_record": str(count_per_page),
            "page": str(page),
            "smb": "",
        }
        response = fetch_with_retries(
            session,
            GOSZAKUP_LOTS_URL,
            params=params,
            timeout=60,
            attempts=6,
            base_sleep=1.0,
        )
        response.encoding = response.encoding or "utf-8"
        return parse_lot_rows(response.text, tru)

    page_1_rows, max_page = get_page(1)
    if not page_1_rows and max_page <= 1:
        return []

    all_rows = list(page_1_rows)
    limited_max_page = min(max_page, max_pages_per_code)
    for page in range(2, limited_max_page + 1):
        page_rows, _ = get_page(page)
        all_rows.extend(page_rows)
        if sleep_between_pages > 0:
            time.sleep(sleep_between_pages)

    # De-duplicate by (lot_number, code) for safety.
    deduped: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for row in all_rows:
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def write_csv(path: Path, rows: Sequence[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)


def chunks(seq: Sequence[TruCode], size: int) -> Iterable[Sequence[TruCode]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU code.")
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--source-gid", type=int, default=0)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--output", default="output/lots_2025_by_tru.csv")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--count-per-page", type=int, default=50)
    parser.add_argument("--max-pages-per-code", type=int, default=200)
    parser.add_argument("--sleep-between-pages", type=float, default=0.0)
    parser.add_argument("--max-codes", type=int, default=0, help="0 means all codes")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    tru_codes = read_tru_codes(args.source_sheet_id, gid=args.source_gid)
    if args.max_codes > 0:
        tru_codes = tru_codes[: args.max_codes]
    total_codes = len(tru_codes)
    print(f"Loaded TRU codes: {total_codes}")
    if total_codes == 0:
        raise RuntimeError("No TRU codes loaded from source sheet.")

    rows: list[list[str]] = []
    done = 0
    started_at = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(
                fetch_code_rows,
                code,
                year=args.year,
                count_per_page=args.count_per_page,
                max_pages_per_code=args.max_pages_per_code,
                sleep_between_pages=args.sleep_between_pages,
            ): code
            for code in tru_codes
        }
        for future in as_completed(future_map):
            code = future_map[future]
            done += 1
            try:
                code_rows = future.result()
            except Exception as err:  # noqa: BLE001
                print(f"[ERROR] {code.code}: {err}", file=sys.stderr)
                continue
            rows.extend(code_rows)
            if done % max(1, args.progress_every) == 0 or done == total_codes:
                elapsed = time.time() - started_at
                print(
                    f"Progress {done}/{total_codes} | rows={len(rows)} | "
                    f"elapsed={elapsed:.1f}s"
                )

    # Global de-duplication for edge cases where one lot can match repeated code rows.
    unique_rows: list[list[str]] = []
    seen_global: set[tuple[str, str]] = set()
    for row in rows:
        key = (row[0], row[1])
        if key in seen_global:
            continue
        seen_global.add(key)
        unique_rows.append(row)

    output_path = Path(args.output)
    write_csv(output_path, unique_rows)
    print(f"Done. Saved rows: {len(unique_rows)}")
    print(f"CSV path: {output_path.resolve()}")
    print(
        "Note: direct write to Google Sheets requires credentials. "
        "Use this CSV for import into the target sheet."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
