#!/usr/bin/env python3
import argparse
import csv
import io
import json
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

SOURCE_CODES_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
DEFAULT_OUTPUT_CSV = "/workspace/lots_2025_status360_amount15m.csv"
GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

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


@dataclass
class CodeTask:
    code: str
    item_name: str


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_number_spaces(text: str) -> str:
    return normalize_text((text or "").replace("\xa0", " "))


def extract_cell_primary_text(td) -> str:
    strong = td.find("strong")
    if strong:
        return clean_number_spaces(strong.get_text(" ", strip=True))
    link = td.find("a")
    if link:
        return clean_number_spaces(link.get_text(" ", strip=True))
    return clean_number_spaces(td.get_text(" ", strip=True))


def load_codes(source: str) -> List[CodeTask]:
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source, timeout=120)
        response.raise_for_status()
        decoded = response.content.decode("utf-8-sig", errors="replace")
    else:
        decoded = Path(source).read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        return []

    data_rows = rows[1:] if rows else []
    out: List[CodeTask] = []
    seen = set()
    for row in data_rows:
        if not row:
            continue
        code = normalize_text(row[0] if len(row) > 0 else "")
        item_name = normalize_text(row[1] if len(row) > 1 else "")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(CodeTask(code=code, item_name=item_name))
    return out


def parse_total_records(soup: BeautifulSoup) -> int:
    max_total = 0
    for info in soup.select("div.dataTables_info"):
        text = normalize_text(info.get_text(" ", strip=True))
        match = re.search(r"из\s+([\d\s]+)\s+запис", text, flags=re.IGNORECASE)
        if not match:
            continue
        total = int(match.group(1).replace(" ", ""))
        if total > max_total:
            max_total = total
    return max_total


def parse_result_rows(soup: BeautifulSoup, code: str, item_name: str) -> List[List[str]]:
    table = soup.find("table", id="search-result")
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    result_rows: List[List[str]] = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = extract_cell_primary_text(tds[0])
        announcement_name = extract_cell_primary_text(tds[1])
        lot_name_desc = clean_number_spaces(tds[2].get_text(" ", strip=True))
        lot_name_desc = normalize_text(re.sub(r"\bИстория\b", " ", lot_name_desc, flags=re.IGNORECASE))
        quantity = clean_number_spaces(tds[3].get_text(" ", strip=True))
        amount = clean_number_spaces(tds[4].get_text(" ", strip=True))
        trade_method = clean_number_spaces(tds[5].get_text(" ", strip=True))
        status = clean_number_spaces(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        result_rows.append(
            [
                lot_number,
                code,
                item_name,
                announcement_name,
                lot_name_desc,
                quantity,
                amount,
                trade_method,
                status,
            ]
        )
    return result_rows


def make_base_params(code: str, year: int, amount_from: Optional[int], status: Optional[str]) -> Dict[str, str]:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[customer]": "",
        "filter[amount_from]": str(amount_from) if amount_from is not None else "",
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": str(year),
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": "",
        "page": "",
    }
    if status:
        params["filter[status][]"] = status
    return params


def fetch_code_rows(
    task: CodeTask,
    year: int,
    amount_from: Optional[int],
    status: Optional[str],
    count_record: int,
    max_retries: int,
    retry_backoff: float,
) -> Tuple[List[List[str]], int]:
    params = make_base_params(task.code, year, amount_from, status)
    params["count_record"] = str(count_record)
    all_rows: List[List[str]] = []
    expected_total = 0

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
        )

        # First page to detect total.
        first_soup = None
        for attempt in range(max_retries + 1):
            try:
                response = session.get(GOSZAKUP_SEARCH_URL, params=params, timeout=60)
                response.raise_for_status()
                first_soup = BeautifulSoup(response.text, "lxml")
                break
            except Exception:
                if attempt >= max_retries:
                    raise
                sleep_for = retry_backoff * (2**attempt) + random.uniform(0.0, 0.6)
                time.sleep(sleep_for)
        if first_soup is None:
            raise RuntimeError("failed to fetch first page")

        expected_total = parse_total_records(first_soup)
        all_rows.extend(parse_result_rows(first_soup, task.code, task.item_name))

        if expected_total <= count_record:
            return all_rows, expected_total

        total_pages = max(1, math.ceil(expected_total / count_record))

        for page in range(2, total_pages + 1):
            page_params = dict(params)
            page_params["page"] = str(page)
            page_rows = None
            for attempt in range(max_retries + 1):
                try:
                    response = session.get(GOSZAKUP_SEARCH_URL, params=page_params, timeout=60)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "lxml")
                    page_rows = parse_result_rows(soup, task.code, task.item_name)
                    break
                except Exception:
                    if attempt >= max_retries:
                        raise
                    sleep_for = retry_backoff * (2**attempt) + random.uniform(0.0, 0.6)
                    time.sleep(sleep_for)
            if page_rows:
                all_rows.extend(page_rows)

    return all_rows, expected_total


def write_rows_csv(rows: List[List[str]], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_COLUMNS)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect goszakup 2025 lots by TRU codes.")
    parser.add_argument("--source-csv-url", default=SOURCE_CODES_SHEET_CSV)
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", default="360", help="goszakup status code, use empty string to disable")
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15000000,
        help="minimum lot amount filter, use -1 to disable",
    )
    parser.add_argument("--count-record", type=int, default=500)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-backoff", type=float, default=1.2)
    parser.add_argument("--limit-codes", type=int, default=0, help="0 = all codes")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--failures-json", default="/workspace/goszakup_failures.json")
    args = parser.parse_args()

    amount_from = None if args.amount_from is not None and args.amount_from < 0 else args.amount_from
    status = args.status.strip() if args.status is not None else ""
    if not status:
        status = None

    tasks = load_codes(args.source_csv_url)
    if args.limit_codes > 0:
        tasks = tasks[: args.limit_codes]

    print(f"Loaded TRU codes: {len(tasks)}")
    print(
        "Filters:",
        json.dumps(
            {
                "year": args.year,
                "status": status,
                "amount_from": amount_from,
                "count_record": args.count_record,
                "workers": args.workers,
            },
            ensure_ascii=False,
        ),
    )

    all_rows: List[List[str]] = []
    failures: List[Dict[str, str]] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                fetch_code_rows,
                task,
                args.year,
                amount_from,
                status,
                args.count_record,
                args.max_retries,
                args.retry_backoff,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            completed += 1
            try:
                rows, expected_total = future.result()
                all_rows.extend(rows)
                if completed % max(1, args.progress_every) == 0:
                    print(
                        f"[{completed}/{len(tasks)}] "
                        f"code={task.code} expected={expected_total} rows={len(rows)} "
                        f"total_rows={len(all_rows)}"
                    )
            except Exception as exc:
                failures.append({"code": task.code, "error": str(exc)})
                print(f"[FAIL] code={task.code} error={exc}")

    # De-duplicate exact repeats (can happen on retries / broad TRU overlaps).
    deduped_rows = []
    seen = set()
    for row in all_rows:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        deduped_rows.append(row)

    write_rows_csv(deduped_rows, args.output_csv)
    print(f"Saved CSV: {args.output_csv}")
    print(f"Rows raw={len(all_rows)} deduped={len(deduped_rows)} failures={len(failures)}")

    with open(args.failures_json, "w", encoding="utf-8") as f:
        json.dump(failures, f, ensure_ascii=False, indent=2)
    print(f"Saved failures log: {args.failures_json}")


if __name__ == "__main__":
    main()
