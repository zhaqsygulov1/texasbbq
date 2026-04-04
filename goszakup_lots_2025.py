#!/usr/bin/env python3
"""
Build a lot list for 2025 by TRU codes from a Google Sheet.

Source table format:
  - Код ТРУ
  - Название

Output columns:
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
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID_DEFAULT = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def strip_history_marker(value: str) -> str:
    # The site appends "История" link text into the lot description cell.
    return re.sub(r"\s*История\s*$", "", value, flags=re.IGNORECASE).strip()


def fetch_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: Dict[str, str],
    timeout: int,
    retries: int,
    backoff_base: float,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries - 1:
                break
            delay = backoff_base * (2**attempt)
            time.sleep(delay)
    raise RuntimeError(f"Request failed after {retries} retries: {last_error}")


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for link in soup.select("ul.pagination a[href]"):
        href = link.get("href", "")
        parsed = urlparse(href)
        page_values = parse_qs(parsed.query).get("page", [])
        for page_value in page_values:
            if page_value.isdigit():
                max_page = max(max_page, int(page_value))
    return max_page


def parse_lot_rows(
    html: str, *, tru_code: str, product_name: str
) -> Tuple[List[List[str]], int]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="search-result")
    if not table:
        return [], 1

    rows: List[List[str]] = []
    tr_list = table.find_all("tr")
    for tr in tr_list[1:]:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        values = [normalize_text(td.get_text(" ", strip=True)) for td in tds[:7]]
        values[2] = strip_history_marker(values[2])

        rows.append(
            [
                values[0],
                tru_code,
                product_name,
                values[1],
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
            ]
        )

    return rows, parse_max_page(soup)


@dataclass
class ScrapeResult:
    code: str
    product_name: str
    rows: List[List[str]]
    pages: int
    truncated: bool
    error: Optional[str] = None


def scrape_code(
    code: str,
    product_name: str,
    *,
    year: int,
    status: str,
    amount_from: str,
    count_record: int,
    timeout: int,
    retries: int,
    backoff_base: float,
) -> ScrapeResult:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )

    base_params = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "filter[status][]": status,
        "filter[amount_from]": amount_from,
        "count_record": str(count_record),
        "smb": "",
    }

    try:
        html = fetch_with_retries(
            session,
            GOSZAKUP_SEARCH_URL,
            params={**base_params, "page": "1"},
            timeout=timeout,
            retries=retries,
            backoff_base=backoff_base,
        )
        first_rows, max_page = parse_lot_rows(
            html, tru_code=code, product_name=product_name
        )
        all_rows = list(first_rows)
        last_page_count = len(first_rows)

        for page in range(2, max_page + 1):
            html_page = fetch_with_retries(
                session,
                GOSZAKUP_SEARCH_URL,
                params={**base_params, "page": str(page)},
                timeout=timeout,
                retries=retries,
                backoff_base=backoff_base,
            )
            page_rows, _ = parse_lot_rows(html_page, tru_code=code, product_name=product_name)
            last_page_count = len(page_rows)
            all_rows.extend(page_rows)

        # The portal usually caps large searches to 10,000 rows.
        truncated = max_page >= 100 and last_page_count >= count_record
        return ScrapeResult(
            code=code,
            product_name=product_name,
            rows=all_rows,
            pages=max_page,
            truncated=truncated,
        )
    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(
            code=code,
            product_name=product_name,
            rows=[],
            pages=0,
            truncated=False,
            error=str(exc),
        )


def google_sheet_csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def load_tru_codes(sheet_id: str, gid: str, timeout: int, retries: int) -> List[Tuple[str, str]]:
    session = requests.Session()
    url = google_sheet_csv_url(sheet_id, gid)
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            text = response.content.decode("utf-8-sig", errors="replace")
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header:
                raise RuntimeError("Source table is empty.")
            rows: List[Tuple[str, str]] = []
            for row in reader:
                if not row:
                    continue
                code = row[0].strip()
                name = row[1].strip() if len(row) > 1 else ""
                if code:
                    rows.append((code, name))
            return rows
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries - 1:
                break
            time.sleep(1.5 * (2**attempt))
    raise RuntimeError(f"Failed to load source TRU sheet: {last_error}")


def load_existing_keys(path: str) -> Tuple[Set[Tuple[str, str]], Dict[str, int]]:
    if not os.path.exists(path):
        return set(), {}
    seen: Set[Tuple[str, str]] = set()
    counts: Dict[str, int] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as infile:
        reader = csv.reader(infile)
        next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            key = (row[0].strip(), row[1].strip())
            if key[0] and key[1]:
                seen.add(key)
                counts[key[1]] = counts.get(key[1], 0) + 1
    return seen, counts


def load_state(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as infile:
        return json.load(infile)


def save_state(path: str, state: Dict[str, object]) -> None:
    state["updated_at"] = now_iso()
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as outfile:
        json.dump(state, outfile, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def ensure_output_header(path: str) -> None:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as outfile:
        writer = csv.writer(outfile)
        writer.writerow(OUTPUT_HEADERS)


def append_rows(path: str, rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        return
    with open(path, "a", encoding="utf-8-sig", newline="") as outfile:
        writer = csv.writer(outfile)
        writer.writerows(rows)


def write_missing_codes(
    path: str,
    source_rows: Sequence[Tuple[str, str]],
    found_codes: Set[str],
) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as outfile:
        writer = csv.writer(outfile)
        writer.writerow(["Код ТРУ", "Название"])
        for code, name in source_rows:
            if code not in found_codes:
                writer.writerow([code, name])


def maybe_upload_csv_to_google_sheet(
    csv_path: str,
    sheet_id: str,
    worksheet_name: Optional[str],
    credentials_path: Optional[str],
    chunk_size: int,
) -> None:
    try:
        import gspread
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "gspread is not installed. Install requirements before upload mode."
        ) from exc

    if credentials_path:
        gc = gspread.service_account(filename=credentials_path)
    else:
        env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not env_path:
            raise RuntimeError(
                "Google credentials are missing. Set --google-credentials-file "
                "or GOOGLE_APPLICATION_CREDENTIALS."
            )
        gc = gspread.service_account(filename=env_path)

    spreadsheet = gc.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(worksheet_name) if worksheet_name else spreadsheet.get_worksheet(0)

    worksheet.clear()

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as infile:
        reader = csv.reader(infile)
        chunk: List[List[str]] = []
        row_index = 1
        for row in reader:
            chunk.append(row)
            if len(chunk) >= chunk_size:
                end_row = row_index + len(chunk) - 1
                worksheet.update(f"A{row_index}:I{end_row}", chunk, value_input_option="RAW")
                row_index = end_row + 1
                chunk = []
        if chunk:
            end_row = row_index + len(chunk) - 1
            worksheet.update(f"A{row_index}:I{end_row}", chunk, value_input_option="RAW")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect goszakup 2025 lots by TRU codes from Google Sheet."
    )
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID_DEFAULT)
    parser.add_argument("--source-gid", default="0")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", default="360")
    parser.add_argument("--amount-from", default="15000000")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--count-record", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--backoff-base", type=float, default=1.5)
    parser.add_argument("--output-csv", default="lots_2025_by_tru.csv")
    parser.add_argument("--state-file", default="lots_2025_state.json")
    parser.add_argument("--missing-codes-csv", default="lots_2025_missing_codes.csv")
    parser.add_argument("--summary-json", default="lots_2025_summary.json")
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dedupe", action="store_true")

    parser.add_argument("--upload-to-google-sheet", action="store_true")
    parser.add_argument("--target-sheet-id", default=TARGET_SHEET_ID_DEFAULT)
    parser.add_argument("--target-worksheet-name", default=None)
    parser.add_argument("--google-credentials-file", default=None)
    parser.add_argument("--google-upload-chunk-size", type=int, default=1000)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = now_iso()

    print(f"[{started_at}] Loading source TRU codes...")
    source_rows = load_tru_codes(
        sheet_id=args.source_sheet_id,
        gid=args.source_gid,
        timeout=args.timeout,
        retries=args.retries,
    )
    if args.limit_codes > 0:
        source_rows = source_rows[: args.limit_codes]
    print(f"Loaded {len(source_rows)} TRU codes.")

    ensure_output_header(args.output_csv)

    state = load_state(args.state_file) if args.resume else {}
    completed_codes = set(state.get("completed_codes", [])) if args.resume else set()
    failed_codes: List[str] = list(state.get("failed_codes", [])) if args.resume else []
    truncated_codes: List[str] = list(state.get("truncated_codes", [])) if args.resume else []

    seen_keys: Set[Tuple[str, str]] = set()
    found_counts: Dict[str, int] = {}
    if args.dedupe or args.resume:
        seen_keys, found_counts = load_existing_keys(args.output_csv)
        if seen_keys:
            print(f"Loaded {len(seen_keys)} existing rows for dedupe/resume.")

    pending_rows = [(code, name) for code, name in source_rows if code not in completed_codes]
    print(
        f"Pending codes: {len(pending_rows)} "
        f"(already completed from state: {len(completed_codes)})."
    )

    if not pending_rows:
        print("No pending codes. Proceeding to summary/export steps.")

    futures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        for code, name in pending_rows:
            futures.append(
                executor.submit(
                    scrape_code,
                    code,
                    name,
                    year=args.year,
                    status=args.status,
                    amount_from=args.amount_from,
                    count_record=args.count_record,
                    timeout=args.timeout,
                    retries=args.retries,
                    backoff_base=args.backoff_base,
                )
            )

        done_counter = 0
        for future in as_completed(futures):
            result = future.result()
            done_counter += 1

            if result.error:
                failed_codes.append(result.code)
                print(
                    f"[{done_counter}/{len(futures)}] FAIL {result.code}: {result.error}",
                    file=sys.stderr,
                )
            else:
                rows_to_write = result.rows
                if args.dedupe:
                    filtered_rows: List[List[str]] = []
                    for row in result.rows:
                        key = (row[0], row[1])
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        filtered_rows.append(row)
                    rows_to_write = filtered_rows

                append_rows(args.output_csv, rows_to_write)
                completed_codes.add(result.code)
                if result.rows:
                    found_counts[result.code] = found_counts.get(result.code, 0) + len(result.rows)

                if result.truncated and result.code not in truncated_codes:
                    truncated_codes.append(result.code)

                print(
                    f"[{done_counter}/{len(futures)}] OK {result.code}: "
                    f"rows={len(result.rows)} pages={result.pages}"
                    + (" TRUNCATED_10000" if result.truncated else "")
                )

            if done_counter % 20 == 0 or done_counter == len(futures):
                state = {
                    "source_sheet_id": args.source_sheet_id,
                    "source_gid": args.source_gid,
                    "year": args.year,
                    "status": args.status,
                    "amount_from": args.amount_from,
                    "count_record": args.count_record,
                    "workers": args.workers,
                    "completed_codes": sorted(completed_codes),
                    "failed_codes": sorted(set(failed_codes)),
                    "truncated_codes": sorted(set(truncated_codes)),
                    "found_counts": found_counts,
                    "updated_at": now_iso(),
                }
                save_state(args.state_file, state)

    source_codes = {code for code, _ in source_rows}
    found_codes = {code for code, count in found_counts.items() if count > 0}
    missing_codes = sorted(source_codes - found_codes)

    write_missing_codes(args.missing_codes_csv, source_rows, found_codes)

    summary = {
        "started_at": started_at,
        "finished_at": now_iso(),
        "source_sheet_id": args.source_sheet_id,
        "target_sheet_id": args.target_sheet_id,
        "filters": {
            "year": args.year,
            "status": args.status,
            "amount_from": args.amount_from,
            "count_record": args.count_record,
        },
        "source_code_count": len(source_rows),
        "codes_with_rows": len(found_codes),
        "codes_without_rows": len(missing_codes),
        "failed_codes_count": len(set(failed_codes)),
        "truncated_codes_count": len(set(truncated_codes)),
        "output_csv": args.output_csv,
        "state_file": args.state_file,
        "missing_codes_csv": args.missing_codes_csv,
        "failed_codes": sorted(set(failed_codes)),
        "truncated_codes": sorted(set(truncated_codes)),
    }
    with open(args.summary_json, "w", encoding="utf-8") as outfile:
        json.dump(summary, outfile, ensure_ascii=False, indent=2)

    save_state(
        args.state_file,
        {
            "source_sheet_id": args.source_sheet_id,
            "source_gid": args.source_gid,
            "year": args.year,
            "status": args.status,
            "amount_from": args.amount_from,
            "count_record": args.count_record,
            "workers": args.workers,
            "completed_codes": sorted(completed_codes),
            "failed_codes": sorted(set(failed_codes)),
            "truncated_codes": sorted(set(truncated_codes)),
            "found_counts": found_counts,
            "updated_at": now_iso(),
        },
    )

    print(
        "Summary: "
        f"codes_total={len(source_rows)}, codes_with_rows={len(found_codes)}, "
        f"codes_without_rows={len(missing_codes)}, failed={len(set(failed_codes))}, "
        f"truncated={len(set(truncated_codes))}"
    )

    if args.upload_to_google_sheet:
        print("Uploading CSV to target Google Sheet...")
        maybe_upload_csv_to_google_sheet(
            csv_path=args.output_csv,
            sheet_id=args.target_sheet_id,
            worksheet_name=args.target_worksheet_name,
            credentials_path=args.google_credentials_file,
            chunk_size=max(1, args.google_upload_chunk_size),
        )
        print("Upload complete.")
    else:
        print(
            "Upload step skipped. Use --upload-to-google-sheet with valid "
            "Google credentials to write into Google Sheets."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
