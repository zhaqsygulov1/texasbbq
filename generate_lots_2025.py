#!/usr/bin/env python3
"""Build 2025 lots report by TRU codes from Google Sheets.

Input:
  - Source sheet with columns: "Код ТРУ", "Название"
Output CSV columns:
  - № лота, Код ТРУ, Наименование товара, Наименование объявления,
    Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Iterable

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
LOTS_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

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

RE_TOTAL = re.compile(r"из\s+([\d\s]+)\s+записей", flags=re.IGNORECASE)
RE_PAGE = re.compile(r"[?&]page=(\d+)")


def norm(text: str) -> str:
    return " ".join((text or "").split())


def parse_int_ru(value: str) -> int:
    cleaned = re.sub(r"[^\d]", "", value or "")
    return int(cleaned) if cleaned else 0


def retry_get(session: requests.Session, url: str, params: dict, attempts: int = 6) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=(20, 120))
            resp.raise_for_status()
            if "Реестр лотов" not in resp.text:
                raise RuntimeError("Unexpected response body (lots page marker not found)")
            return resp.text
        except Exception as exc:  # pylint: disable=broad-except
            last_exc = exc
            if attempt == attempts:
                break
            sleep_s = 2 ** (attempt - 1)
            time.sleep(sleep_s)
    raise RuntimeError(f"GET failed after {attempts} attempts: {params}") from last_exc


def parse_total_records(soup: BeautifulSoup) -> int:
    info = soup.select_one(".dataTables_info")
    if not info:
        return 0
    match = RE_TOTAL.search(info.get_text(" ", strip=True))
    if not match:
        return 0
    return parse_int_ru(match.group(1))


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for anchor in soup.select("ul.pagination a[href]"):
        href = anchor.get("href", "")
        page_match = RE_PAGE.search(href)
        if page_match:
            max_page = max(max_page, int(page_match.group(1)))
    return max_page


def parse_rows(html_text: str, code: str, product_name: str) -> tuple[list[list[str]], int, int]:
    soup = BeautifulSoup(html_text, "lxml")
    total_records = parse_total_records(soup)
    max_page = parse_max_page(soup)

    rows: list[list[str]] = []
    tbody_rows = soup.select("#search-result tbody tr")
    for tr in tbody_rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = norm(tds[0].get_text(" ", strip=True))
        announce_name = norm(tds[1].get_text(" ", strip=True))
        lot_desc = norm(tds[2].get_text(" ", strip=True))
        qty = norm(tds[3].get_text(" ", strip=True))
        amount = norm(tds[4].get_text(" ", strip=True))
        method = norm(tds[5].get_text(" ", strip=True))
        status = norm(tds[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_number,
                code,
                product_name,
                announce_name,
                lot_desc,
                qty,
                amount,
                method,
                status,
            ]
        )

    return rows, total_records, max_page


@dataclass
class CodeResult:
    code: str
    rows: list[list[str]]
    total_records: int
    page_count: int
    ok: bool
    error: str = ""


def fetch_code_rows(code: str, product_name: str, year: int, count_record: int) -> CodeResult:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en;q=0.9",
        }
    )

    try:
        all_rows: list[list[str]] = []
        base_params = {
            "filter[enstru]": code,
            "filter[year]": str(year),
            "count_record": str(count_record),
        }

        html_page_1 = retry_get(session, LOTS_SEARCH_URL, {**base_params, "page": "1"})
        page_rows, total_records, max_page = parse_rows(html_page_1, code, product_name)
        all_rows.extend(page_rows)

        if max_page > 1:
            for page in range(2, max_page + 1):
                html_text = retry_get(session, LOTS_SEARCH_URL, {**base_params, "page": str(page)})
                page_rows, _, _ = parse_rows(html_text, code, product_name)
                all_rows.extend(page_rows)

        return CodeResult(
            code=code,
            rows=all_rows,
            total_records=total_records,
            page_count=max_page,
            ok=True,
        )
    except Exception as exc:  # pylint: disable=broad-except
        return CodeResult(
            code=code,
            rows=[],
            total_records=0,
            page_count=0,
            ok=False,
            error=str(exc),
        )


def load_tru_codes(source_csv_url: str) -> list[tuple[str, str]]:
    resp = requests.get(source_csv_url, timeout=(20, 120))
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig")

    rows: list[tuple[str, str]] = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        code = (row.get("Код ТРУ") or "").strip()
        name = (row.get("Название") or "").strip()
        if not code:
            continue
        rows.append((code, name))
    return rows


def write_errors(path: Path, errors: list[CodeResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Код ТРУ", "Ошибка"])
        for item in errors:
            writer.writerow([item.code, item.error])


def build_report(
    output_csv_path: Path,
    source_csv_url: str = SOURCE_SHEET_CSV,
    year: int = 2025,
    workers: int = 6,
    count_record: int = 2000,
    limit_codes: int | None = None,
) -> tuple[int, int, int]:
    start_ts = time.time()
    print("Loading TRU codes...", flush=True)
    tru_rows = load_tru_codes(source_csv_url)
    if limit_codes is not None:
        tru_rows = tru_rows[:limit_codes]
    total_codes = len(tru_rows)
    print(f"Codes loaded: {total_codes}", flush=True)

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    processed = 0
    ok_codes = 0
    failed_codes = 0
    written_rows = 0
    seen: set[tuple[str, str]] = set()
    lock = Lock()
    errors: list[CodeResult] = []

    with output_csv_path.open("w", encoding="utf-8-sig", newline="") as out_fh:
        writer = csv.writer(out_fh)
        writer.writerow(OUTPUT_HEADER)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(fetch_code_rows, code, name, year, count_record)
                for code, name in tru_rows
            ]

            for future in as_completed(futures):
                result: CodeResult = future.result()
                processed += 1

                if not result.ok:
                    failed_codes += 1
                    errors.append(result)
                else:
                    ok_codes += 1
                    fresh_rows: list[list[str]] = []
                    for row in result.rows:
                        # Defensive dedupe by (lot_number, code).
                        key = (row[0], row[1])
                        if key in seen:
                            continue
                        seen.add(key)
                        fresh_rows.append(row)
                    if fresh_rows:
                        writer.writerows(fresh_rows)
                        written_rows += len(fresh_rows)

                if processed % 25 == 0 or processed == total_codes:
                    elapsed = time.time() - start_ts
                    rate = processed / elapsed if elapsed > 0 else 0
                    eta_s = (total_codes - processed) / rate if rate > 0 else 0
                    eta_m = eta_s / 60
                    print(
                        (
                            f"[{processed}/{total_codes}] rows={written_rows} "
                            f"ok={ok_codes} failed={failed_codes} eta={eta_m:.1f}m"
                        ),
                        flush=True,
                    )

                # Keep writes and set updates thread-safe if run model changes in future.
                with lock:
                    pass

    if errors:
        err_path = output_csv_path.with_suffix(".errors.csv")
        write_errors(err_path, errors)
        print(f"Errors saved: {err_path}", flush=True)

    elapsed = time.time() - start_ts
    print(
        f"Done in {elapsed/60:.1f}m. Output rows: {written_rows}. "
        f"Codes ok: {ok_codes}. Codes failed: {failed_codes}.",
        flush=True,
    )
    return written_rows, ok_codes, failed_codes


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate lots report for TRU codes")
    parser.add_argument(
        "--output",
        default="lots_2025_by_tru.csv",
        help="Output CSV path (default: lots_2025_by_tru.csv)",
    )
    parser.add_argument(
        "--source-csv-url",
        default=SOURCE_SHEET_CSV,
        help="Source TRU sheet CSV URL",
    )
    parser.add_argument(
        "--year",
        default=2025,
        type=int,
        help="Financial year filter (default: 2025)",
    )
    parser.add_argument(
        "--workers",
        default=6,
        type=int,
        help="Parallel workers for TRU codes (default: 6)",
    )
    parser.add_argument(
        "--count-record",
        default=2000,
        type=int,
        help="Rows per page on goszakup (default: 2000)",
    )
    parser.add_argument(
        "--limit-codes",
        default=None,
        type=int,
        help="Process only first N codes (debug option)",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    output_path = Path(args.output).resolve()
    build_report(
        output_csv_path=output_path,
        source_csv_url=args.source_csv_url,
        year=args.year,
        workers=args.workers,
        count_record=args.count_record,
        limit_codes=args.limit_codes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
