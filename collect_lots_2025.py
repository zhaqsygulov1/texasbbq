#!/usr/bin/env python3
"""Collect Kazakhstan public lots for a list of TRU codes (2025).

The script reads TRU codes from a Google Sheet (CSV export), queries
https://goszakup.gov.kz/ru/search/lots for every code, parses all pages, and
writes rows into a CSV file with columns:

№ лота, Код ТРУ, Наименование товара, Наименование объявления,
Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

INPUT_SHEET_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

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

STATUS_IDS_FOR_SPLIT = [
    "190",
    "210",
    "220",
    "230",
    "240",
    "245",
    "250",
    "260",
    "270",
    "280",
    "310",
    "320",
    "325",
    "330",
    "360",
    "370",
    "410",
    "420",
    "430",
    "440",
    "444",
    "445",
    "460",
    "510",
    "540",
    "550",
]


def build_google_sheet_csv_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def normalize_space(value: str) -> str:
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_int_from_text(value: str) -> int:
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else 0


def create_retry_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )
    return session


_thread_local = threading.local()


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_retry_session()
        _thread_local.session = session
    return session


@dataclass
class TruCode:
    code: str
    name: str


@dataclass
class LotRow:
    lot_number: str
    tru_code: str
    tru_name: str
    announce_name: str
    lot_name_description: str
    quantity: str
    amount_tenge: str
    method: str
    status: str

    def to_csv_row(self) -> list[str]:
        return [
            self.lot_number,
            self.tru_code,
            self.tru_name,
            self.announce_name,
            self.lot_name_description,
            self.quantity,
            self.amount_tenge,
            self.method,
            self.status,
        ]


def read_codes(sheet_id: str, timeout: int) -> list[TruCode]:
    url = build_google_sheet_csv_url(sheet_id)
    response = create_retry_session().get(url, timeout=timeout)
    response.raise_for_status()

    decoded = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(decoded.splitlines())
    rows: list[TruCode] = []
    for row in reader:
        code = normalize_space(row.get("Код ТРУ", ""))
        name = normalize_space(row.get("Название", ""))
        if not code:
            continue
        rows.append(TruCode(code=code, name=name))
    return rows


def parse_lot_rows(html: str, code: str, code_name: str) -> tuple[list[LotRow], int]:
    soup = BeautifulSoup(html, "html.parser")

    info_text = ""
    info_node = soup.select_one("div.dataTables_info strong")
    if info_node:
        info_text = normalize_space(info_node.get_text(" ", strip=True))
    total_match = re.search(r"из\s+([\d\s]+)\s+запис", info_text)
    total_records = parse_int_from_text(total_match.group(1)) if total_match else 0

    table_rows = soup.select("table#search-result tbody tr")
    parsed_rows: list[LotRow] = []

    for tr in table_rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = normalize_space(tds[0].get_text(" ", strip=True))

        announce_strong = tds[1].find("strong")
        announce_name = (
            normalize_space(announce_strong.get_text(" ", strip=True))
            if announce_strong
            else normalize_space(tds[1].get_text(" ", strip=True))
        )

        lot_strings = [normalize_space(s) for s in tds[2].stripped_strings]
        lot_strings = [s for s in lot_strings if s and s.lower() != "история"]
        lot_name_description = normalize_space(" ".join(lot_strings))

        quantity = normalize_space(tds[3].get_text(" ", strip=True))
        amount_tenge = normalize_space(tds[4].get_text(" ", strip=True))
        method = normalize_space(tds[5].get_text(" ", strip=True))
        status = normalize_space(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        parsed_rows.append(
            LotRow(
                lot_number=lot_number,
                tru_code=code,
                tru_name=code_name,
                announce_name=announce_name,
                lot_name_description=lot_name_description,
                quantity=quantity,
                amount_tenge=amount_tenge,
                method=method,
                status=status,
            )
        )

    return parsed_rows, total_records


def fetch_page_html(
    code: str,
    year: int,
    page: int,
    count_record: int,
    timeout: int,
    month: int | None = None,
    status_id: str | None = None,
) -> str:
    params = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
    }
    if month is not None:
        params["filter[month]"] = str(month)
    if status_id is not None:
        params["filter[status][]"] = status_id
    # Manual URL helps when server strips array-like keys in some clients.
    url = f"{SEARCH_URL}?{urlencode(params, doseq=True)}"

    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = get_session().get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_for = min(2**attempt, 20)
            time.sleep(sleep_for)

    raise RuntimeError(f"Failed to fetch page for code={code}, page={page}: {last_error}")


def fetch_query_first_page(
    tru: TruCode,
    year: int,
    count_record: int,
    timeout: int,
    month: int | None,
    status_id: str | None,
) -> tuple[list[LotRow], int]:
    first_html = fetch_page_html(
        code=tru.code,
        year=year,
        page=1,
        count_record=count_record,
        timeout=timeout,
        month=month,
        status_id=status_id,
    )
    first_rows, total_records = parse_lot_rows(first_html, tru.code, tru.name)

    return first_rows, total_records


def fetch_query_all_pages(
    tru: TruCode,
    year: int,
    count_record: int,
    timeout: int,
    month: int | None,
    status_id: str | None,
    first_rows: list[LotRow] | None = None,
    total_records: int | None = None,
) -> list[LotRow]:
    if first_rows is None or total_records is None:
        first_rows, total_records = fetch_query_first_page(
            tru=tru,
            year=year,
            count_record=count_record,
            timeout=timeout,
            month=month,
            status_id=status_id,
        )

    if total_records <= count_record:
        return list(first_rows)

    total_pages = math.ceil(total_records / count_record)
    all_rows = list(first_rows)
    for page in range(2, total_pages + 1):
        html = fetch_page_html(
            code=tru.code,
            year=year,
            page=page,
            count_record=count_record,
            timeout=timeout,
            month=month,
            status_id=status_id,
        )
        rows, _ = parse_lot_rows(html, tru.code, tru.name)
        all_rows.extend(rows)
    return all_rows


def dedupe_rows(rows: list[LotRow]) -> list[LotRow]:
    unique: dict[tuple[str, str, str], LotRow] = {}
    for row in rows:
        key = (row.lot_number, row.announce_name, row.lot_name_description)
        unique[key] = row
    return list(unique.values())


def collect_for_code(
    tru: TruCode,
    year: int,
    count_record: int,
    timeout: int,
    split_threshold: int,
) -> list[LotRow]:
    base_first_rows, base_total = fetch_query_first_page(
        tru=tru,
        year=year,
        count_record=count_record,
        timeout=timeout,
        month=None,
        status_id=None,
    )
    if base_total < split_threshold:
        return fetch_query_all_pages(
            tru=tru,
            year=year,
            count_record=count_record,
            timeout=timeout,
            month=None,
            status_id=None,
            first_rows=base_first_rows,
            total_records=base_total,
        )

    split_rows: list[LotRow] = []
    for month in range(1, 13):
        month_first_rows, month_total = fetch_query_first_page(
            tru=tru,
            year=year,
            count_record=count_record,
            timeout=timeout,
            month=month,
            status_id=None,
        )
        if month_total == 0:
            continue
        if month_total < split_threshold:
            month_rows = fetch_query_all_pages(
                tru=tru,
                year=year,
                count_record=count_record,
                timeout=timeout,
                month=month,
                status_id=None,
                first_rows=month_first_rows,
                total_records=month_total,
            )
            split_rows.extend(month_rows)
            continue

        for status_id in STATUS_IDS_FOR_SPLIT:
            status_first_rows, status_total = fetch_query_first_page(
                tru=tru,
                year=year,
                count_record=count_record,
                timeout=timeout,
                month=month,
                status_id=status_id,
            )
            if status_total == 0:
                continue
            status_rows = fetch_query_all_pages(
                tru=tru,
                year=year,
                count_record=count_record,
                timeout=timeout,
                month=month,
                status_id=status_id,
                first_rows=status_first_rows,
                total_records=status_total,
            )
            split_rows.extend(status_rows)

    deduped = dedupe_rows(split_rows)
    return deduped if deduped else base_rows


def load_processed_codes(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()
    processed: set[str] = set()
    with state_file.open("r", encoding="utf-8") as fp:
        for line in fp:
            code = normalize_space(line)
            if code:
                processed.add(code)
    return processed


def append_processed_code(state_file: Path, code: str) -> None:
    with state_file.open("a", encoding="utf-8") as fp:
        fp.write(f"{code}\n")


def write_header_if_needed(path: Path, append: bool) -> None:
    if append and path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)


def append_rows(path: Path, rows: Iterable[LotRow]) -> int:
    count = 0
    with path.open("a", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        for row in rows:
            writer.writerow(row.to_csv_row())
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-sheet-id", default=INPUT_SHEET_DEFAULT)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--output", default="lots_2025_by_tru.csv")
    parser.add_argument("--state-file", default="lots_2025_progress.txt")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Process only first N codes (0 = all).",
    )
    parser.add_argument(
        "--only-codes-file",
        default="",
        help="Optional path to file with one TRU code per line.",
    )
    parser.add_argument(
        "--split-threshold",
        type=int,
        default=10000,
        help="If total query results are >= threshold, split by month/status.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore previous state and overwrite output CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    state_path = Path(args.state_file)

    codes = read_codes(args.input_sheet_id, timeout=args.timeout)
    if args.only_codes_file:
        wanted = set()
        with Path(args.only_codes_file).open("r", encoding="utf-8") as fp:
            for line in fp:
                code = normalize_space(line)
                if code:
                    wanted.add(code)
        codes = [c for c in codes if c.code in wanted]

    if args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    processed_codes = set()
    append_mode = not args.restart
    if append_mode:
        processed_codes = load_processed_codes(state_path)

    write_header_if_needed(output_path, append=append_mode)

    pending_codes = [c for c in codes if c.code not in processed_codes]
    total = len(codes)
    print(f"Всего кодов: {total}. Уже обработано: {len(processed_codes)}. Осталось: {len(pending_codes)}")

    if not pending_codes:
        print("Новых кодов для обработки нет.")
        return

    io_lock = threading.Lock()
    done = 0
    written_rows = 0
    started_at = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[Future[list[LotRow]], TruCode] = {
            pool.submit(
                collect_for_code,
                tru,
                args.year,
                args.count_record,
                args.timeout,
                args.split_threshold,
            ): tru
            for tru in pending_codes
        }

        for future in as_completed(futures):
            tru = futures[future]
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] Код {tru.code}: {exc}")
                continue

            with io_lock:
                just_written = append_rows(output_path, rows)
                append_processed_code(state_path, tru.code)
                written_rows += just_written
                done += 1

            elapsed = time.time() - started_at
            print(
                f"[{done}/{len(pending_codes)}] {tru.code}: {just_written} строк, "
                f"всего записано {written_rows}, {elapsed:.1f} сек."
            )

    print(f"Готово. Итоговый файл: {output_path.resolve()}")
    print(f"Состояние обработки: {state_path.resolve()}")


if __name__ == "__main__":
    main()
