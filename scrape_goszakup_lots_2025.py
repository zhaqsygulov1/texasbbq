#!/usr/bin/env python3
import argparse
import csv
import gzip
import io
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Dict, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
DEST_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
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


TAG_RE = re.compile(r"<[^>]+>", re.S | re.I)
TABLE_RE = re.compile(r'<table[^>]*id=["\']search-result["\'][^>]*>(.*?)</table>', re.S | re.I)
ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
CELL_START_RE = re.compile(r"<td\b[^>]*>", re.S | re.I)


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def first_line(value: str) -> str:
    for line in value.splitlines():
        line = normalize_ws(line)
        if line:
            return line
    return ""


def clean_cell_text(raw_cell_html: str) -> str:
    with_breaks = re.sub(r"<br\s*/?>", "\n", raw_cell_html, flags=re.I)
    no_tags = TAG_RE.sub(" ", with_breaks)
    text = io.StringIO(no_tags).getvalue()
    lines = [normalize_ws(line) for line in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def clean_announce(cell: str) -> str:
    lines = [normalize_ws(line) for line in cell.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    # Usually first non-empty line is the announce title.
    return lines[0]


def clean_lot_desc(cell: str) -> str:
    lines = [normalize_ws(line) for line in cell.splitlines()]
    lines = [ln for ln in lines if ln and ln.lower() != "история"]
    if not lines:
        return ""
    return lines[0]


def parse_total_count(page_html: str) -> int | None:
    m = re.search(r"Показано\s+c\s+\d+\s+по\s+\d+\s+из\s+([\d\s]+)\s+записей", page_html, re.IGNORECASE)
    if not m:
        return None
    num = re.sub(r"\s+", "", m.group(1))
    try:
        return int(num)
    except ValueError:
        return None


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_source_codes(sheet_csv_url: str) -> List[Tuple[str, str]]:
    resp = requests.get(sheet_csv_url, timeout=60)
    resp.raise_for_status()
    # Keep utf-8 default; fallback for broken content.
    text = resp.content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    data_rows = rows[1:]
    result: List[Tuple[str, str]] = []
    for row in data_rows:
        if not row:
            continue
        code = normalize_ws(row[0]) if len(row) >= 1 else ""
        name = normalize_ws(row[1]) if len(row) >= 2 else ""
        if code:
            result.append((code, name))
    return result


def parse_rows_from_html(page_html: str) -> List[List[str]]:
    table_match = TABLE_RE.search(page_html)
    if not table_match:
        return []

    table_html = table_match.group(1)
    rows: List[List[str]] = []
    for row_match in ROW_RE.finditer(table_html):
        row_html = row_match.group(1)
        cell_starts = list(CELL_START_RE.finditer(row_html))
        if len(cell_starts) < 7:
            continue

        cells_raw: List[str] = []
        for idx, start_match in enumerate(cell_starts):
            content_start = start_match.end()
            content_end = (
                cell_starts[idx + 1].start() if idx + 1 < len(cell_starts) else len(row_html)
            )
            cells_raw.append(row_html[content_start:content_end])

        row = [clean_cell_text(raw_cell) for raw_cell in cells_raw[:7]]

        if len(row) < 7:
            continue

        lot_no = first_line(row[0])
        announce = clean_announce(row[1])
        lot_desc = clean_lot_desc(row[2])
        qty = first_line(row[3])
        amount = first_line(row[4])
        method = first_line(row[5])
        status = first_line(row[6])
        if not lot_no or lot_no == "№ лота":
            continue
        rows.append([lot_no, announce, lot_desc, qty, amount, method, status])

    return rows


def fetch_lots_for_code(
    session: requests.Session,
    code: str,
    item_name: str,
    year: int,
    count_record: int,
    delay: float,
    max_pages: int | None = None,
) -> Tuple[List[List[str]], int]:
    page = 1
    all_rows: List[List[str]] = []
    total_hint: int | None = None
    while True:
        params = {
            "filter[enstru]": code,
            "filter[year]": str(year),
            "count_record": str(count_record),
            "page": str(page),
        }
        resp = session.get(GOSZAKUP_SEARCH_URL, params=params, timeout=90)
        resp.raise_for_status()
        page_html = resp.text

        if total_hint is None:
            total_hint = parse_total_count(page_html)

        parsed_rows = parse_rows_from_html(page_html)
        if not parsed_rows:
            break

        for r in parsed_rows:
            all_rows.append([r[0], code, item_name, r[1], r[2], r[3], r[4], r[5], r[6]])

        if max_pages is not None and page >= max_pages:
            break

        if total_hint is not None and len(all_rows) >= total_hint:
            break

        # Defensive stop for malformed pagination.
        if len(parsed_rows) < min(10, count_record):
            break

        page += 1
        if delay > 0:
            time.sleep(delay)

    return all_rows, total_hint or 0


def try_clear_and_append_google_sheet(sheet_id: str, rows: List[List[str]]) -> Tuple[bool, str]:
    """Best-effort unauthenticated call to show explicit permission outcome."""
    clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:Z:clear"
    append_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:append"
        "?valueInputOption=RAW&insertDataOption=OVERWRITE"
    )
    body_clear = "{}"
    body_append = {"majorDimension": "ROWS", "values": rows}
    try:
        c = requests.post(clear_url, json={}, timeout=30)
        a = requests.post(append_url, json=body_append, timeout=60)
        ok = c.status_code < 300 and a.status_code < 300
        msg = f"clear={c.status_code}, append={a.status_code}, append_text={a.text[:300]}"
        return ok, msg
    except Exception as exc:  # pragma: no cover - operational
        return False, f"exception: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape goszakup lots by TRU codes for selected year.")
    parser.add_argument("--source-csv-url", default=SOURCE_SHEET_CSV)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--max-pages-per-code", type=int, default=0)
    parser.add_argument("--output-csv", default="lots_2025_by_tru.csv")
    parser.add_argument("--output-csv-gz", default="lots_2025_by_tru.csv.gz")
    parser.add_argument("--summary-file", default="lots_2025_summary.txt")
    parser.add_argument("--attempt-upload-sheet", action="store_true")
    args = parser.parse_args()

    pairs = fetch_source_codes(args.source_csv_url)
    if not pairs:
        print("No source codes fetched.", file=sys.stderr)
        return 1

    # Deduplicate by code preserving first item name.
    code_to_name: Dict[str, str] = {}
    for code, name in pairs:
        if code not in code_to_name:
            code_to_name[code] = name
    codes = list(code_to_name.items())

    if args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    output_path = Path(args.output_csv)
    output_gz_path = Path(args.output_csv_gz)
    summary_path = Path(args.summary_file)

    session = build_session()
    lock = Lock()
    total_rows = 0
    success_codes = 0
    failed_codes: List[Tuple[str, str]] = []
    total_codes = len(codes)

    max_pages = args.max_pages_per_code if args.max_pages_per_code > 0 else None

    with output_path.open("w", encoding="utf-8", newline="") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(OUTPUT_HEADERS)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_to_code = {
                pool.submit(
                    fetch_lots_for_code,
                    session,
                    code,
                    item_name,
                    args.year,
                    args.count_record,
                    args.delay,
                    max_pages,
                ): (code, item_name)
                for code, item_name in codes
            }

            done = 0
            for future in as_completed(future_to_code):
                code, _ = future_to_code[future]
                done += 1
                try:
                    rows, total_hint = future.result()
                    with lock:
                        for row in rows:
                            writer.writerow(row)
                    total_rows += len(rows)
                    success_codes += 1
                    print(
                        f"[{done}/{total_codes}] code={code} rows={len(rows)} total_rows={total_rows} hint={total_hint}",
                        flush=True,
                    )
                except Exception as exc:
                    failed_codes.append((code, str(exc)))
                    print(f"[{done}/{total_codes}] code={code} FAILED: {exc}", flush=True)

    # Gzip copy for compact artifact.
    with output_path.open("rb") as src, gzip.open(output_gz_path, "wb", compresslevel=6) as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)

    summary_lines = [
        f"year={args.year}",
        f"source_codes_raw={len(pairs)}",
        f"source_codes_unique={len(code_to_name)}",
        f"processed_codes={len(codes)}",
        f"success_codes={success_codes}",
        f"failed_codes={len(failed_codes)}",
        f"output_rows={total_rows}",
        f"output_csv={output_path.resolve()}",
        f"output_csv_gz={output_gz_path.resolve()}",
    ]

    if failed_codes:
        summary_lines.append("failed_code_samples:")
        for code, err in failed_codes[:30]:
            summary_lines.append(f"- {code}: {err}")

    upload_status = "not_attempted"
    if args.attempt_upload_sheet:
        # Do not attempt to upload huge sheet in one request.
        # This intentionally checks whether public unauthenticated write is available.
        ok, msg = try_clear_and_append_google_sheet(
            DEST_SHEET_ID,
            [OUTPUT_HEADERS, ["TEST", "TEST", "TEST", "TEST", "TEST", "1", "1", "TEST", "TEST"]],
        )
        upload_status = f"ok={ok}; {msg}"
        summary_lines.append(f"sheet_upload_attempt={upload_status}")

    summary_text = "\n".join(summary_lines) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    print(summary_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
