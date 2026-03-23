#!/usr/bin/env python3
"""Collect 2025 lots by TRU codes and build append-only delta CSV.

Data sources:
- Source TRU codes sheet (CSV export URL).
- Target result sheet (CSV export URL).
- goszakup lots search pages.

Output:
- /workspace/new_rows_2025.csv (rows missing in target sheet)
- /workspace/run_summary_2025.json (run stats + errors)
"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

SOURCE_CSV_PATH = Path("/workspace/source_codes.csv")
TARGET_CSV_PATH = Path("/workspace/target_sheet.csv")
OUTPUT_NEW_ROWS_PATH = Path("/workspace/new_rows_2025.csv")
OUTPUT_SUMMARY_PATH = Path("/workspace/run_summary_2025.json")

BASE_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

COUNT_RECORD = 2000
MAX_WORKERS = 8
MAX_FETCH_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 40
MAX_ALLOWED_ROWS_PER_CODE = 10000


HEADERS = [
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
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


_thread_local = threading.local()


def norm(text: str) -> str:
    return " ".join(text.split())


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _thread_local.session = session
    return session


def read_source_codes(path: Path) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("Код ТРУ") or "").strip()
            name = (row.get("Название") or "").strip()
            if code:
                rows.append((code, name))
    return rows


def read_existing_keys(path: Path) -> set[Tuple[str, str]]:
    keys: set[Tuple[str, str]] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lot_number = (row.get("№ лота") or "").strip()
            code = (row.get("Код ТРУ") or "").strip()
            if lot_number and code:
                keys.add((lot_number, code))
    return keys


def build_url(code: str, page: int) -> str:
    params = {
        "filter[enstru]": code,
        "filter[status][0]": "360",
        "filter[amount_from]": "15000000",
        "filter[year]": "2025",
        "count_record": str(COUNT_RECORD),
        "page": str(page),
        "smb": "",
    }
    return f"{BASE_SEARCH_URL}?{urlencode(params)}"


def fetch_html(code: str, page: int) -> str:
    url = build_url(code, page)
    session = get_session()
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 200 and "search-result" in response.text:
                return response.text
        except requests.RequestException:
            pass
        time.sleep(1.5 * attempt)
    raise RuntimeError(f"Failed to fetch code={code} page={page}")


def parse_total_count(soup: BeautifulSoup) -> int:
    info = soup.find("div", class_="dataTables_info")
    if not info:
        return 0
    text = norm(info.get_text(" ", strip=True))
    match = re.search(r"из\s+(\d+)\s+записей", text)
    return int(match.group(1)) if match else 0


def parse_applied_enstru_code(soup: BeautifulSoup) -> str:
    field = soup.find("input", id="in_enstru")
    if not field:
        return ""
    return norm(field.get("value", ""))


def parse_result_rows(soup: BeautifulSoup) -> List[Tuple[str, str, str, str, str, str]]:
    table = soup.find("table", id="search-result")
    if not table or not table.tbody:
        return []

    rows: List[Tuple[str, str, str, str, str, str]] = []
    for tr in table.tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue

        lot_number = norm(tds[0].get_text(" ", strip=True))

        ann_strong = tds[1].find("strong")
        announcement = norm(ann_strong.get_text(" ", strip=True)) if ann_strong else norm(
            tds[1].get_text(" ", strip=True)
        )

        lot_text = norm(tds[2].get_text(" ", strip=True))
        lot_text = norm(lot_text.replace("История", ""))

        qty = norm(tds[3].get_text(" ", strip=True))
        amount = norm(tds[4].get_text(" ", strip=True))
        method = norm(tds[5].get_text(" ", strip=True))
        status = norm(tds[6].get_text(" ", strip=True))

        if lot_number:
            rows.append((lot_number, announcement, lot_text, qty, amount, method, status))
    return rows


@dataclass
class CodeResult:
    code: str
    product_name: str
    rows: List[List[str]]
    total_from_site: int
    error: str | None = None


def scrape_code(code: str, product_name: str) -> CodeResult:
    try:
        html = fetch_html(code, page=1)
        soup = BeautifulSoup(html, "html5lib")
        applied_code = parse_applied_enstru_code(soup)
        if applied_code and applied_code != code:
            raise RuntimeError(
                f"Filter mismatch: requested={code} applied={applied_code}"
            )
        total = parse_total_count(soup)
        if total > MAX_ALLOWED_ROWS_PER_CODE:
            raise RuntimeError(
                f"Suspiciously high total for specific code: code={code} total={total}"
            )
        parsed = parse_result_rows(soup)

        all_rows = [
            [lot, code, product_name, ann, lot_desc, qty, amount, method, status]
            for lot, ann, lot_desc, qty, amount, method, status in parsed
        ]

        if total > len(parsed):
            pages = math.ceil(total / COUNT_RECORD)
            for page in range(2, pages + 1):
                page_html = fetch_html(code, page=page)
                page_soup = BeautifulSoup(page_html, "html5lib")
                page_rows = parse_result_rows(page_soup)
                if not page_rows:
                    break
                all_rows.extend(
                    [
                        [lot, code, product_name, ann, lot_desc, qty, amount, method, status]
                        for lot, ann, lot_desc, qty, amount, method, status in page_rows
                    ]
                )

        # Defensive de-duplication in case portal returns duplicated rows.
        deduped_rows: List[List[str]] = []
        seen_lots: set[str] = set()
        for row in all_rows:
            lot_number = row[0]
            if lot_number in seen_lots:
                continue
            seen_lots.add(lot_number)
            deduped_rows.append(row)

        return CodeResult(
            code=code,
            product_name=product_name,
            rows=deduped_rows,
            total_from_site=total,
        )
    except Exception as exc:  # noqa: BLE001
        return CodeResult(
            code=code,
            product_name=product_name,
            rows=[],
            total_from_site=0,
            error=str(exc),
        )


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    started = time.time()
    codes = read_source_codes(SOURCE_CSV_PATH)
    existing_keys = read_existing_keys(TARGET_CSV_PATH)

    print(f"Loaded source codes: {len(codes)}")
    print(f"Loaded existing target keys: {len(existing_keys)}")

    new_rows: List[List[str]] = []
    new_keys_seen: set[Tuple[str, str]] = set()
    total_rows_found = 0
    total_site_rows = 0
    errors: List[Dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(scrape_code, code, name) for code, name in codes]
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()

            if result.error:
                errors.append({"code": result.code, "error": result.error})
            total_site_rows += result.total_from_site
            total_rows_found += len(result.rows)

            for row in result.rows:
                lot_number = row[0]
                code = row[1]
                key = (lot_number, code)
                if key in existing_keys or key in new_keys_seen:
                    continue
                new_keys_seen.add(key)
                new_rows.append(row)

            if done % 50 == 0 or done == len(codes):
                print(
                    f"Progress: {done}/{len(codes)} codes | "
                    f"rows_found={total_rows_found} | new_rows={len(new_rows)} | errors={len(errors)}"
                )

    new_rows.sort(key=lambda r: (r[1], r[0]))
    write_csv(OUTPUT_NEW_ROWS_PATH, HEADERS, new_rows)

    summary = {
        "codes_total": len(codes),
        "codes_with_errors": len(errors),
        "errors": errors[:200],
        "site_total_rows_reported": total_site_rows,
        "rows_parsed": total_rows_found,
        "new_rows": len(new_rows),
        "output_new_rows_csv": str(OUTPUT_NEW_ROWS_PATH),
        "generated_at_epoch": int(time.time()),
        "duration_seconds": round(time.time() - started, 2),
    }
    OUTPUT_SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
