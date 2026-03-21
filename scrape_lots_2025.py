#!/usr/bin/env python3
import argparse
import csv
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv&gid=0"
)
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
SUMMARY_RE = re.compile(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей")
SPACE_RE = re.compile(r"\s+")

thread_local = threading.local()


def clean_text(value: str) -> str:
    return SPACE_RE.sub(" ", (value or "")).strip()


def get_session() -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
                )
            }
        )
        thread_local.session = session
    return session


def reset_session() -> None:
    session = getattr(thread_local, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
    if hasattr(thread_local, "session"):
        delattr(thread_local, "session")


def build_params(code: str, page: int, count_record: int) -> Dict[str, str]:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[status][0]": "360",
        "filter[customer]": "",
        "filter[amount_from]": "15000000",
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": "2025",
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": str(count_record),
    }
    if page > 1:
        params["page"] = str(page)
    return params


def fetch_html(
    code: str,
    page: int,
    count_record: int,
    retries: int,
    throttle_ms_min: int,
    throttle_ms_max: int,
) -> str:
    params = build_params(code=code, page=page, count_record=count_record)
    session = get_session()
    for attempt in range(retries):
        try:
            if throttle_ms_max > 0:
                delay_ms = random.randint(max(0, throttle_ms_min), max(throttle_ms_min, throttle_ms_max))
                time.sleep(delay_ms / 1000.0)
            response = session.get(SEARCH_URL, params=params, timeout=45)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            reset_session()
            session = get_session()
            # Exponential backoff + jitter reduces transient 0-result responses.
            sleep_sec = (2 ** attempt) + random.uniform(0.2, 0.9)
            time.sleep(sleep_sec)
    raise RuntimeError("unreachable")


def parse_total_count(html: str, fallback_rows: int) -> int:
    match = SUMMARY_RE.search(html)
    if not match:
        return fallback_rows
    return int(match.group(1).replace(" ", ""))


def parse_rows(html: str, code: str, product_name: str) -> List[List[str]]:
    soup = BeautifulSoup(html, "lxml")
    rows: List[List[str]] = []

    for tr in soup.select("table#search-result tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 7:
            cells = tr.find_all("td")
        if len(cells) < 7:
            continue

        lot_no = clean_text(cells[0].get_text(" ", strip=True))
        if not lot_no:
            continue

        ann_strong = cells[1].find("strong")
        announcement_name = clean_text(
            ann_strong.get_text(" ", strip=True) if ann_strong else cells[1].get_text(" ", strip=True)
        )

        lot_name_desc = clean_text(cells[2].get_text(" ", strip=True)).replace("История", "").strip()
        quantity = clean_text(cells[3].get_text(" ", strip=True))
        amount = clean_text(cells[4].get_text(" ", strip=True))
        method = clean_text(cells[5].get_text(" ", strip=True))
        status = clean_text(cells[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_no,
                code,
                product_name,
                announcement_name,
                lot_name_desc,
                quantity,
                amount,
                method,
                status,
            ]
        )

    return rows


def fetch_rows_for_code(
    idx: int,
    code: str,
    product_name: str,
    count_record: int,
    retries: int,
    max_pages: int,
    empty_rechecks: int,
    empty_recheck_pause_sec: float,
    throttle_ms_min: int,
    throttle_ms_max: int,
) -> Tuple[int, List[List[str]], int]:
    def fetch_full_once() -> Tuple[List[List[str]], int]:
        html = fetch_html(
            code=code,
            page=1,
            count_record=count_record,
            retries=retries,
            throttle_ms_min=throttle_ms_min,
            throttle_ms_max=throttle_ms_max,
        )
        rows = parse_rows(html=html, code=code, product_name=product_name)
        total = parse_total_count(html=html, fallback_rows=len(rows))
        page_count = max(1, math.ceil(total / count_record)) if total else 1
        page_count = min(page_count, max_pages)

        all_rows = list(rows)
        for page in range(2, page_count + 1):
            page_html = fetch_html(
                code=code,
                page=page,
                count_record=count_record,
                retries=retries,
                throttle_ms_min=throttle_ms_min,
                throttle_ms_max=throttle_ms_max,
            )
            page_rows = parse_rows(html=page_html, code=code, product_name=product_name)
            if not page_rows:
                break
            all_rows.extend(page_rows)
        return all_rows, total

    all_rows, total = fetch_full_once()
    if not all_rows and total == 0:
        for attempt in range(empty_rechecks):
            time.sleep(empty_recheck_pause_sec + random.uniform(0.15, 0.8))
            reset_session()
            refreshed_rows, refreshed_total = fetch_full_once()
            if refreshed_rows or refreshed_total > 0:
                all_rows, total = refreshed_rows, refreshed_total
                break

    return idx, all_rows, total


def load_codes(source_csv: Path) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    with source_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            code = (row[0] if len(row) > 0 else "").strip()
            name = (row[1] if len(row) > 1 else "").strip()
            if code:
                items.append((code, name))
    return items


def maybe_download_source(source_csv: Path) -> None:
    if source_csv.exists():
        return
    response = requests.get(SOURCE_SHEET_CSV_URL, timeout=60)
    response.raise_for_status()
    source_csv.write_bytes(response.content)


def write_table(path: Path, rows: List[List[str]], delimiter: str) -> None:
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
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сбор лотов 2025 по кодам ТРУ из Google-таблицы."
    )
    parser.add_argument("--source-csv", default="source_codes.csv")
    parser.add_argument("--output-csv", default="lots_2025_filtered.csv")
    parser.add_argument("--output-tsv", default="lots_2025_filtered.tsv")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--empty-rechecks", type=int, default=2)
    parser.add_argument("--empty-recheck-pause-sec", type=float, default=1.8)
    parser.add_argument("--throttle-ms-min", type=int, default=80)
    parser.add_argument("--throttle-ms-max", type=int, default=220)
    args = parser.parse_args()

    source_csv = Path(args.source_csv).resolve()
    output_csv = Path(args.output_csv).resolve()
    output_tsv = Path(args.output_tsv).resolve()

    maybe_download_source(source_csv)
    codes = load_codes(source_csv)
    if args.max_codes > 0:
        codes = codes[: args.max_codes]

    total_codes = len(codes)
    print(f"Loaded {total_codes} TRU codes.")

    rows_by_idx: Dict[int, List[List[str]]] = {}
    finished = 0
    found_rows = 0
    codes_with_rows = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                fetch_rows_for_code,
                idx,
                code,
                name,
                args.count_record,
                args.retries,
                args.max_pages,
                args.empty_rechecks,
                args.empty_recheck_pause_sec,
                args.throttle_ms_min,
                args.throttle_ms_max,
            )
            for idx, (code, name) in enumerate(codes)
        ]

        for future in as_completed(futures):
            idx, code_rows, total = future.result()
            rows_by_idx[idx] = code_rows
            finished += 1
            if code_rows:
                codes_with_rows += 1
                found_rows += len(code_rows)
            if finished % 25 == 0 or finished == total_codes:
                print(
                    f"Progress: {finished}/{total_codes}, "
                    f"codes_with_rows={codes_with_rows}, rows={found_rows}, "
                    f"last_total={total}"
                )

    all_rows: List[List[str]] = []
    for idx in range(total_codes):
        all_rows.extend(rows_by_idx.get(idx, []))

    write_table(output_csv, all_rows, delimiter=",")
    write_table(output_tsv, all_rows, delimiter="\t")

    summary_path = output_csv.with_suffix(".summary.txt")
    summary_path.write_text(
        "\n".join(
            [
                f"codes_total={total_codes}",
                f"codes_with_rows={codes_with_rows}",
                f"rows_total={len(all_rows)}",
                f"output_csv={output_csv}",
                f"output_tsv={output_tsv}",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Done. rows_total={len(all_rows)}")
    print(f"CSV: {output_csv}")
    print(f"TSV: {output_tsv}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
