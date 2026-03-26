#!/usr/bin/env python3
"""Collect 2025 lots from goszakup.gov.kz by TRU codes.

Output columns:
    № лота
    Код ТРУ
    Наименование товара
    Наименование объявления
    Наименование и описание лота
    Кол-во
    Сумма, тг.
    Способ закупки
    Статус
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import requests


DEFAULT_SOURCE_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/"
    "edit?usp=sharing"
)
DEFAULT_OUTPUT_CSV = "lots_2025.csv"
DEFAULT_SOURCE_CSV = "source_tru.csv"
DEFAULT_STATE_DIR = ".lots_state"
GOSZAKUP_URL = "https://goszakup.gov.kz/ru/search/lots"

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

TOTAL_RE = re.compile(
    r"Показано\s*c\s*\d+\s*по\s*\d+\s*из\s*([0-9 ]+)\s*записей",
    flags=re.IGNORECASE,
)
TABLE_RE = re.compile(
    r'<table id="search-result".*?<tbody>(.*?)</tbody>',
    flags=re.IGNORECASE | re.DOTALL,
)
ROW_RE = re.compile(r"<tr>(.*?)</tr>", flags=re.IGNORECASE | re.DOTALL)
STRONG_RE = re.compile(r"<strong[^>]*>(.*?)</strong>", flags=re.IGNORECASE | re.DOTALL)
BR_RE = re.compile(r"<br\s*/?>", flags=re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
QTY_RE = re.compile(
    r'<td[^>]*class="text-center"[^>]*>\s*([^<]+?)\s*</td>',
    flags=re.IGNORECASE | re.DOTALL,
)
AMOUNT_RE = re.compile(
    r'<td[^>]*nowrap[^>]*>\s*<strong[^>]*>(.*?)</strong>\s*</td>',
    flags=re.IGNORECASE | re.DOTALL,
)
METHOD_STATUS_RE = re.compile(
    r'<td[^>]*nowrap[^>]*>.*?</td>\s*<td>\s*(.*?)\s*</td>\s*<td>\s*(.*?)\s*</td>',
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect lots from goszakup for TRU codes (2025)."
    )
    parser.add_argument(
        "--source-sheet-url",
        default=DEFAULT_SOURCE_SHEET_URL,
        help="Google Sheets URL with columns: Код ТРУ, Название.",
    )
    parser.add_argument(
        "--source-csv",
        default=DEFAULT_SOURCE_CSV,
        help="Local source CSV path. If missing, script downloads from source sheet URL.",
    )
    parser.add_argument(
        "--output-csv",
        default=DEFAULT_OUTPUT_CSV,
        help="Result CSV path.",
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        help="Directory for resume/checkpoint files.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="Financial year filter.",
    )
    parser.add_argument(
        "--status",
        default="360",
        help="Lot status code filter (360 = Закупка состоялась).",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Minimum amount filter as in source example URL.",
    )
    parser.add_argument(
        "--count-record",
        type=int,
        default=2000,
        help="Rows per page request.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=40,
        help="HTTP timeout (seconds).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Retries per page request.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.12,
        help="Delay between successful HTTP requests.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start index in TRU list (0-based).",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Optional cap of processed codes (0 = all).",
    )
    parser.add_argument(
        "--force-restart",
        action="store_true",
        help="Ignore checkpoint and rebuild output from scratch.",
    )
    return parser.parse_args()


def extract_sheet_id(url: str) -> str:
    marker = "/spreadsheets/d/"
    if marker not in url:
        raise ValueError(f"Cannot extract sheet id from URL: {url}")
    tail = url.split(marker, 1)[1]
    return tail.split("/", 1)[0]


def extract_gid(url: str, default: str = "0") -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "gid" in query and query["gid"]:
        return query["gid"][0]
    if parsed.fragment:
        fragment_qs = parse_qs(parsed.fragment.replace("#", ""))
        if "gid" in fragment_qs and fragment_qs["gid"]:
            return fragment_qs["gid"][0]
    return default


def download_sheet_csv(sheet_url: str, output_path: Path, timeout: int) -> None:
    sheet_id = extract_sheet_id(sheet_url)
    gid = extract_gid(sheet_url, default="0")
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    )
    response = requests.get(
        export_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    output_path.write_bytes(response.content)


def load_tru_codes(source_csv: Path) -> List[Tuple[str, str]]:
    if not source_csv.exists():
        raise FileNotFoundError(f"Source CSV not found: {source_csv}")
    result: List[Tuple[str, str]] = []
    seen = set()
    with source_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            code = (row.get("Код ТРУ") or "").strip()
            name = (row.get("Название") or "").strip()
            if not code:
                continue
            if code in seen:
                continue
            seen.add(code)
            result.append((code, name))
    return result


def clean_text(fragment: str) -> str:
    fragment = BR_RE.sub("\n", fragment)
    fragment = TAG_RE.sub("", fragment)
    fragment = html.unescape(fragment)
    fragment = re.sub(r"\s+", " ", fragment).strip()
    return fragment


def extract_strong_text(fragment: str) -> str:
    match = STRONG_RE.search(fragment)
    return clean_text(match.group(1) if match else fragment)


def parse_total_count(page_html: str) -> int:
    match = TOTAL_RE.search(page_html)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_rows(page_html: str, tru_code: str, tru_name: str) -> List[Dict[str, str]]:
    table_match = TABLE_RE.search(page_html)
    if not table_match:
        return []
    tbody = table_match.group(1)
    output: List[Dict[str, str]] = []
    seen_lots: set[str] = set()
    for row_html in ROW_RE.findall(tbody):
        strong_values = [clean_text(v) for v in STRONG_RE.findall(row_html)]
        if len(strong_values) < 3:
            continue
        lot_number = strong_values[0]
        announce_name = strong_values[1]
        lot_name = strong_values[2]
        qty_match = QTY_RE.search(row_html)
        amount_match = AMOUNT_RE.search(row_html)
        ms_match = METHOD_STATUS_RE.search(row_html)
        quantity = clean_text(qty_match.group(1)) if qty_match else ""
        amount = clean_text(amount_match.group(1)) if amount_match else ""
        method = clean_text(ms_match.group(1)) if ms_match else ""
        status = clean_text(ms_match.group(2)) if ms_match else ""
        if not lot_number:
            continue
        row_key = f"{lot_number}|{tru_code}"
        if row_key in seen_lots:
            continue
        seen_lots.add(row_key)
        output.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": tru_code,
                "Наименование товара": tru_name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_name,
                "Кол-во": quantity,
                "Сумма, тг.": amount,
                "Способ закупки": method,
                "Статус": status,
            }
        )
    return output


def request_page_with_retry(
    session: requests.Session,
    params: Dict[str, str],
    timeout: int,
    max_retries: int,
) -> str:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = session.get(
                GOSZAKUP_URL,
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:  # requests exceptions are not always typed consistently
            last_error = exc
            backoff = min(2 ** attempt, 15)
            time.sleep(backoff)
    raise RuntimeError(f"Request failed after {max_retries} retries: {last_error}")


def ensure_output_header(output_csv: Path, force_restart: bool) -> None:
    if force_restart and output_csv.exists():
        output_csv.unlink()
    if output_csv.exists() and output_csv.stat().st_size > 0:
        return
    with output_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()


def load_done_codes(done_path: Path, force_restart: bool) -> set:
    if force_restart and done_path.exists():
        done_path.unlink()
    if not done_path.exists():
        return set()
    done = set()
    for line in done_path.read_text(encoding="utf-8").splitlines():
        code = line.strip()
        if code:
            done.add(code)
    return done


def append_done_code(done_path: Path, code: str) -> None:
    with done_path.open("a", encoding="utf-8") as fh:
        fh.write(code + "\n")


def append_rows(output_csv: Path, rows: Sequence[Dict[str, str]]) -> None:
    if not rows:
        return
    with output_csv.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writerows(rows)


def iter_codes(
    codes: Sequence[Tuple[str, str]], start_index: int, limit_codes: int
) -> Iterable[Tuple[int, str, str]]:
    if start_index < 0:
        start_index = 0
    subset = list(codes[start_index:])
    if limit_codes > 0:
        subset = subset[:limit_codes]
    for idx, (code, name) in enumerate(subset, start=start_index):
        yield idx, code, name


def main() -> int:
    args = parse_args()
    source_csv = Path(args.source_csv)
    output_csv = Path(args.output_csv)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    done_path = state_dir / "done_codes.txt"
    stats_path = state_dir / "stats.txt"

    if not source_csv.exists():
        print(f"[info] Source CSV not found, downloading from: {args.source_sheet_url}")
        download_sheet_csv(args.source_sheet_url, source_csv, timeout=args.request_timeout)

    codes = load_tru_codes(source_csv)
    print(f"[info] Loaded {len(codes)} unique TRU codes.")

    ensure_output_header(output_csv, force_restart=args.force_restart)
    done_codes = load_done_codes(done_path, force_restart=args.force_restart)
    print(f"[info] Loaded checkpoint: {len(done_codes)} codes already processed.")

    session = requests.Session()
    total_rows = 0
    processed_codes = 0
    skipped_codes = 0
    started_at = time.time()

    for idx, code, name in iter_codes(codes, args.start_index, args.limit_codes):
        if code in done_codes:
            skipped_codes += 1
            continue

        base_params = {
            "filter[enstru]": code,
            "filter[status][0]": str(args.status),
            "filter[amount_from]": str(args.amount_from),
            "filter[year]": str(args.year),
            "count_record": str(args.count_record),
            "smb": "",
        }
        try:
            params = dict(base_params)
            params["page"] = "0"
            first_page = request_page_with_retry(
                session=session,
                params=params,
                timeout=args.request_timeout,
                max_retries=args.max_retries,
            )
            total_count = parse_total_count(first_page)
            pages = max(1, (total_count + args.count_record - 1) // args.count_record)
            pages = min(pages, 100)

            all_rows = parse_rows(first_page, code, name)
            for page in range(1, pages):
                params = dict(base_params)
                params["page"] = str(page)
                page_html = request_page_with_retry(
                    session=session,
                    params=params,
                    timeout=args.request_timeout,
                    max_retries=args.max_retries,
                )
                all_rows.extend(parse_rows(page_html, code, name))
                time.sleep(args.sleep_seconds)

            append_rows(output_csv, all_rows)
            append_done_code(done_path, code)
            done_codes.add(code)
            processed_codes += 1
            total_rows += len(all_rows)
            elapsed = time.time() - started_at
            print(
                f"[ok] idx={idx} code={code} total={total_count} "
                f"parsed={len(all_rows)} processed={processed_codes} "
                f"elapsed={elapsed:.1f}s"
            )
        except Exception as exc:
            print(f"[error] idx={idx} code={code} -> {exc}", file=sys.stderr)

        time.sleep(args.sleep_seconds)

    elapsed = time.time() - started_at
    summary = (
        f"processed_codes={processed_codes}\n"
        f"skipped_codes={skipped_codes}\n"
        f"total_rows_written={total_rows}\n"
        f"elapsed_seconds={elapsed:.2f}\n"
        f"output_csv={output_csv.resolve()}\n"
        f"done_codes_file={done_path.resolve()}\n"
    )
    stats_path.write_text(summary, encoding="utf-8")
    print("[done]")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
