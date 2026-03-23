#!/usr/bin/env python3
"""
Collect 2025 lots from goszakup by ENSTRU codes from Google Sheet.

Output columns:
№ лота, Код ТРУ, Наименование товара, Наименование объявления,
Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import StringIO
from typing import Iterable, List, Sequence, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEFAULT_OUTPUT_FILE = "lots_2025_by_tru_codes.csv"
GOSZAKUP_URL = "https://goszakup.gov.kz/ru/search/lots"
PAGE_SIZE = 500
MAX_TOTAL_ROWS_PER_CODE = 10000  # portal-side cap visible in UI
REQUEST_TIMEOUT = 60
MAX_RETRIES = 6
MAX_WORKERS = 8

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

_print_lock = threading.Lock()


@dataclass
class CodeRow:
    code: str
    item_name: str


@dataclass
class CodeScrapeResult:
    code: str
    item_name: str
    rows: List[List[str]]
    estimated_total: int
    pages_loaded: int
    error: str | None = None


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def build_source_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        }
    )
    return session


def fetch_url(session: requests.Session, url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_seconds = min(2 * (2 ** (attempt - 1)), 30) + random.uniform(0.0, 0.8)
            log(f"[retry {attempt}/{MAX_RETRIES}] URL failed: {url} | error: {exc}")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Failed to fetch URL after retries: {url} | {last_error}")


def read_codes_from_source_sheet(sheet_id: str) -> List[CodeRow]:
    session = make_session()
    url = build_source_csv_url(sheet_id)
    content = session.get(url, timeout=REQUEST_TIMEOUT).content
    decoded = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(decoded))
    rows: List[CodeRow] = []
    for row in reader:
        code = (row.get("Код ТРУ") or "").strip()
        item_name = (row.get("Название") or "").strip()
        if not code:
            continue
        rows.append(CodeRow(code=code, item_name=item_name))
    return rows


def build_search_url(code: str, year: int, page: int, count_record: int) -> str:
    params = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
    }
    return f"{GOSZAKUP_URL}?{urlencode(params)}"


def parse_total_count(soup: BeautifulSoup) -> int:
    info = soup.select_one(".dataTables_info strong")
    if not info:
        return 0
    text = info.get_text(" ", strip=True)
    # Example: "Показано c 1 по 500 из 10000 записей"
    match = re.search(r"из\s+([\d\s]+)", text)
    if not match:
        return 0
    try:
        return int(match.group(1).replace(" ", ""))
    except ValueError:
        return 0


def parse_last_page_from_pagination(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.select("ul.pagination a[href]"):
        href = a.get("href") or ""
        match = re.search(r"(?:[?&])page=(\d+)", href)
        if match:
            max_page = max(max_page, int(match.group(1)))
    return max_page


def clean_lot_title(text: str) -> str:
    # "История" is a button label rendered inside the same table cell.
    return re.sub(r"\s*История\s*$", "", text).strip()


def parse_lot_table_rows(
    soup: BeautifulSoup,
    code: str,
    item_name: str,
) -> List[List[str]]:
    table = soup.select_one("table.dataTable")
    if not table:
        return []

    out_rows: List[List[str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.select("td")
        if len(tds) < 7:
            continue

        lot_number = tds[0].get_text(" ", strip=True)
        announcement_name = tds[1].get_text(" ", strip=True)
        lot_name_desc = clean_lot_title(tds[2].get_text(" ", strip=True))
        qty = tds[3].get_text(" ", strip=True)
        amount = tds[4].get_text(" ", strip=True)
        procurement_method = tds[5].get_text(" ", strip=True)
        status = tds[6].get_text(" ", strip=True)

        if not lot_number:
            continue

        out_rows.append(
            [
                lot_number,
                code,
                item_name,
                announcement_name,
                lot_name_desc,
                qty,
                amount,
                procurement_method,
                status,
            ]
        )
    return out_rows


def scrape_single_code(session: requests.Session, code_row: CodeRow, year: int) -> CodeScrapeResult:
    code = code_row.code
    item_name = code_row.item_name

    first_url = build_search_url(code=code, year=year, page=1, count_record=PAGE_SIZE)
    try:
        first_html = fetch_url(session, first_url)
        soup = BeautifulSoup(first_html, "lxml")
        all_rows = parse_lot_table_rows(soup, code=code, item_name=item_name)

        estimated_total = parse_total_count(soup)
        estimated_total = min(estimated_total, MAX_TOTAL_ROWS_PER_CODE) if estimated_total else 0

        total_pages = 1
        pages_from_count = 1
        if estimated_total > 0:
            pages_from_count = math.ceil(estimated_total / PAGE_SIZE)
        pages_from_pagination = parse_last_page_from_pagination(soup)
        total_pages = max(1, pages_from_count, pages_from_pagination)

        pages_loaded = 1
        for page in range(2, total_pages + 1):
            page_url = build_search_url(code=code, year=year, page=page, count_record=PAGE_SIZE)
            html = fetch_url(session, page_url)
            page_soup = BeautifulSoup(html, "lxml")
            all_rows.extend(parse_lot_table_rows(page_soup, code=code, item_name=item_name))
            pages_loaded += 1

            # small jitter to reduce risk of temporary blocking
            time.sleep(random.uniform(0.03, 0.15))

        return CodeScrapeResult(
            code=code,
            item_name=item_name,
            rows=all_rows,
            estimated_total=estimated_total,
            pages_loaded=pages_loaded,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        return CodeScrapeResult(
            code=code,
            item_name=item_name,
            rows=[],
            estimated_total=0,
            pages_loaded=0,
            error=str(exc),
        )


def scrape_all_codes(
    code_rows: Sequence[CodeRow],
    year: int,
    output_csv_path: str,
    max_workers: int,
) -> Tuple[int, int, int]:
    total_rows_written = 0
    successful_codes = 0
    failed_codes = 0

    with open(output_csv_path, "w", encoding="utf-8-sig", newline="") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(OUTPUT_HEADERS)

        def worker(code_row: CodeRow) -> CodeScrapeResult:
            session = make_session()
            return scrape_single_code(session, code_row=code_row, year=year)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(worker, code_row): code_row for code_row in code_rows}
            processed = 0
            total_codes = len(code_rows)

            for future in as_completed(futures):
                processed += 1
                code_row = futures[future]
                result = future.result()

                if result.error:
                    failed_codes += 1
                    log(
                        f"[{processed}/{total_codes}] ERROR code={code_row.code} "
                        f"pages={result.pages_loaded} msg={result.error}"
                    )
                    continue

                successful_codes += 1
                if result.rows:
                    writer.writerows(result.rows)
                    total_rows_written += len(result.rows)

                log(
                    f"[{processed}/{total_codes}] code={code_row.code} "
                    f"pages={result.pages_loaded} rows={len(result.rows)} "
                    f"estimated_total={result.estimated_total} written={total_rows_written}"
                )

    return total_rows_written, successful_codes, failed_codes


def col_to_a1(col_num: int) -> str:
    if col_num < 1:
        raise ValueError("Column number must be >= 1")
    result = []
    n = col_num
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def upload_csv_to_google_sheet(
    csv_path: str,
    spreadsheet_id: str,
    worksheet_name: str | None,
    service_account_file: str | None,
    service_account_json_env: str,
    chunk_rows: int,
) -> None:
    try:
        import gspread
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("gspread is not installed; cannot upload to Google Sheets.") from exc

    client = None
    if service_account_file:
        client = gspread.service_account(filename=service_account_file)
    else:
        raw_json = os.getenv(service_account_json_env, "").strip()
        if raw_json:
            import json

            client = gspread.service_account_from_dict(json.loads(raw_json))

    if client is None:
        raise RuntimeError(
            "No Google credentials found. Provide --service-account-file or set "
            f"{service_account_json_env} with service account JSON."
        )

    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name) if worksheet_name else spreadsheet.sheet1
    worksheet.clear()

    log(
        f"Uploading CSV to Google Sheet {spreadsheet_id}, "
        f"worksheet='{worksheet.title}', chunk_rows={chunk_rows}"
    )

    end_col = col_to_a1(len(OUTPUT_HEADERS))
    row_cursor = 1
    uploaded_rows = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f_in:
        reader = csv.reader(f_in)
        buffer: List[List[str]] = []
        for row in reader:
            buffer.append(row)
            if len(buffer) >= chunk_rows:
                end_row = row_cursor + len(buffer) - 1
                range_name = f"A{row_cursor}:{end_col}{end_row}"
                worksheet.update(range_name, buffer, value_input_option="RAW")
                uploaded_rows += len(buffer)
                log(f"Uploaded {uploaded_rows} rows...")
                row_cursor = end_row + 1
                buffer = []

        if buffer:
            end_row = row_cursor + len(buffer) - 1
            range_name = f"A{row_cursor}:{end_col}{end_row}"
            worksheet.update(range_name, buffer, value_input_option="RAW")
            uploaded_rows += len(buffer)
            log(f"Uploaded {uploaded_rows} rows...")

    log(f"Google Sheets upload complete: {uploaded_rows} rows written.")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU codes")
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID, help="Google Sheet ID with columns 'Код ТРУ', 'Название'")
    parser.add_argument("--year", type=int, default=2025, help="Year filter for lots")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="Output CSV file path")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Number of concurrent workers")
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Optional limit for number of codes (for testing). 0 means all codes.",
    )
    parser.add_argument("--target-sheet-id", default="", help="Optional target Google Sheet ID to upload results")
    parser.add_argument("--target-worksheet-name", default="", help="Optional target worksheet name inside Google Sheet")
    parser.add_argument("--service-account-file", default="", help="Path to Google service account JSON file")
    parser.add_argument(
        "--service-account-json-env",
        default="GOOGLE_SERVICE_ACCOUNT_JSON",
        help="Env var name containing service account JSON payload",
    )
    parser.add_argument("--upload-chunk-rows", type=int, default=1000, help="Rows per write request during Google Sheets upload")
    parser.add_argument(
        "--upload-existing-csv-only",
        action="store_true",
        help="Skip scraping and upload already prepared --output CSV to target Google Sheet",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    target_sheet_id = args.target_sheet_id.strip()

    if args.upload_existing_csv_only:
        if not target_sheet_id:
            raise RuntimeError("--upload-existing-csv-only requires --target-sheet-id")
        if not os.path.exists(args.output):
            raise RuntimeError(f"CSV file does not exist: {args.output}")
        upload_csv_to_google_sheet(
            csv_path=args.output,
            spreadsheet_id=target_sheet_id,
            worksheet_name=args.target_worksheet_name.strip() or None,
            service_account_file=args.service_account_file.strip() or None,
            service_account_json_env=args.service_account_json_env.strip(),
            chunk_rows=max(1, args.upload_chunk_rows),
        )
        return 0

    code_rows = read_codes_from_source_sheet(args.source_sheet_id)
    if args.limit_codes > 0:
        code_rows = code_rows[: args.limit_codes]

    log(f"Loaded {len(code_rows)} TRU codes from source sheet.")
    log(f"Scraping lots for year={args.year}, page_size={PAGE_SIZE}, workers={args.workers}")

    total_rows, ok_codes, bad_codes = scrape_all_codes(
        code_rows=code_rows,
        year=args.year,
        output_csv_path=args.output,
        max_workers=max(1, args.workers),
    )

    log("")
    log("Done.")
    log(f"Output CSV: {args.output}")
    log(f"Rows written: {total_rows}")
    log(f"Codes successful: {ok_codes}")
    log(f"Codes failed: {bad_codes}")

    if target_sheet_id:
        upload_csv_to_google_sheet(
            csv_path=args.output,
            spreadsheet_id=target_sheet_id,
            worksheet_name=args.target_worksheet_name.strip() or None,
            service_account_file=args.service_account_file.strip() or None,
            service_account_json_env=args.service_account_json_env.strip(),
            chunk_rows=max(1, args.upload_chunk_rows),
        )

    return 0 if bad_codes == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
