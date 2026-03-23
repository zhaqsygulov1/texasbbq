#!/usr/bin/env python3
"""
Collect 2025 lots from goszakup by TRU codes from a Google Sheet.

Input sheet columns:
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
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


INPUT_SHEET_ID_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
OUTPUT_FILE_DEFAULT = "output/lots_2025_by_tru.csv"
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) lots-collector/1.0"}
LOT_TABLE_HEADERS = {
    "№ лота",
    "Наименование объявления",
    "Наименование и описание лота",
    "Кол-во",
    "Сумма, тг.",
    "Способ закупки",
    "Статус",
}

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


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # In lot column, goszakup often appends a "История" action.
    text = re.sub(r"\bИстория\b", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text


def fetch_tru_codes(sheet_id: str, timeout: int = 60) -> List[TruCode]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    response = requests.get(url, timeout=timeout, headers=HEADERS)
    response.raise_for_status()

    reader = csv.DictReader(response.text.splitlines())
    result: List[TruCode] = []
    seen = set()

    for row in reader:
        code_raw = (row.get("Код ТРУ") or "").strip()
        name_raw = (row.get("Название") or "").strip()
        if not code_raw:
            continue
        if code_raw in seen:
            continue
        seen.add(code_raw)
        result.append(TruCode(code=code_raw, name=name_raw))
    return result


def extract_total_records(page_text: str) -> int:
    match = re.search(r"Показано c \d+ по \d+ из ([\d ]+) записей", page_text)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def find_lot_table(soup: BeautifulSoup):
    for table in soup.select("table.table"):
        headers = {normalize_text(th.get_text()) for th in table.select("thead th")}
        if LOT_TABLE_HEADERS.issubset(headers):
            return table
    return None


def parse_lot_rows(table, tru_code: TruCode) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        items.append(
            {
                "№ лота": normalize_text(tds[0].get_text(" ", strip=True)),
                "Код ТРУ": tru_code.code,
                "Наименование товара": tru_code.name,
                "Наименование объявления": normalize_text(tds[1].get_text(" ", strip=True)),
                "Наименование и описание лота": normalize_text(tds[2].get_text(" ", strip=True)),
                "Кол-во": normalize_text(tds[3].get_text(" ", strip=True)),
                "Сумма, тг.": normalize_text(tds[4].get_text(" ", strip=True)),
                "Способ закупки": normalize_text(tds[5].get_text(" ", strip=True)),
                "Статус": normalize_text(tds[6].get_text(" ", strip=True)),
            }
        )
    return items


def fetch_page(
    session: requests.Session,
    tru_code: str,
    year: int,
    amount_from: int | None,
    status: str | None,
    count_record: int,
    page: int,
    timeout: int = 60,
) -> str:
    params = {
        "filter[enstru]": tru_code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
        "smb": "",
    }
    if amount_from is not None:
        params["filter[amount_from]"] = str(amount_from)
    if status:
        params["filter[status][]"] = status

    response = session.get(BASE_URL, params=params, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_parsed_rows_with_retry(
    session: requests.Session,
    tru_code: TruCode,
    year: int,
    amount_from: int | None,
    status: str | None,
    count_record: int,
    page: int,
    expected_non_empty: bool,
    attempts: int = 6,
) -> Tuple[List[Dict[str, str]], int]:
    last_error = "unknown parser error"
    for attempt in range(1, attempts + 1):
        try:
            html = fetch_page(
                session=session,
                tru_code=tru_code.code,
                year=year,
                amount_from=amount_from,
                status=status,
                count_record=count_record,
                page=page,
            )
            soup = BeautifulSoup(html, "lxml")
            table = find_lot_table(soup)
            if table is None:
                raise RuntimeError("lot table not found")

            rows = parse_lot_rows(table, tru_code)
            total_records = extract_total_records(soup.get_text(" ", strip=True))

            # For pages after first one, empty rows usually mean a transient bad response.
            if expected_non_empty and not rows:
                raise RuntimeError("empty rows on non-empty expected page")

            return rows, total_records
        except Exception as exc:  # pragma: no cover - network/page instability handling
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(0.5 * attempt, 3.0))

    raise RuntimeError(
        f"Unable to parse code={tru_code.code} page={page} after {attempts} attempts: {last_error}"
    )


def collect_for_code(
    tru_code: TruCode,
    year: int,
    amount_from: int | None,
    status: str | None,
    count_record: int,
    max_pages_per_code: int | None,
) -> Tuple[List[Dict[str, str]], int]:
    session = build_session()
    first_rows, total_records = fetch_parsed_rows_with_retry(
        session=session,
        tru_code=tru_code,
        year=year,
        amount_from=amount_from,
        status=status,
        count_record=count_record,
        page=1,
        expected_non_empty=False,
    )
    if total_records == 0:
        return first_rows, len(first_rows)

    total_pages = max(1, math.ceil(total_records / count_record))
    if max_pages_per_code is not None:
        total_pages = min(total_pages, max_pages_per_code)

    rows = list(first_rows)

    for page in range(2, total_pages + 1):
        page_rows, _ = fetch_parsed_rows_with_retry(
            session=session,
            tru_code=tru_code,
            year=year,
            amount_from=amount_from,
            status=status,
            count_record=count_record,
            page=page,
            expected_non_empty=True,
        )
        rows.extend(page_rows)
        # Gentle delay to reduce chance of being rate-limited.
        time.sleep(0.12)

    return rows, total_records


def unique_rows(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        key = (row["№ лота"], row["Код ТРУ"])
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def write_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in OUTPUT_COLUMNS})


def write_failed_codes(path: Path, failed_codes: List[TruCode]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for code in failed_codes:
            f.write(f"{code.code}\t{code.name}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU codes")
    parser.add_argument("--input-sheet-id", default=INPUT_SHEET_ID_DEFAULT)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", default="360", help="goszakup status code, empty to disable")
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15000000,
        help="Minimum amount filter from goszakup example; use 0 or negative to disable",
    )
    parser.add_argument("--count-record", type=int, default=50)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-codes", type=int, default=0, help="0 = all codes")
    parser.add_argument("--max-pages-per-code", type=int, default=0, help="0 = no page limit")
    parser.add_argument("--output", default=OUTPUT_FILE_DEFAULT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    status = args.status.strip() if args.status else None
    amount_from = args.amount_from if args.amount_from and args.amount_from > 0 else None
    max_pages_per_code = args.max_pages_per_code if args.max_pages_per_code > 0 else None

    tru_codes = fetch_tru_codes(args.input_sheet_id)
    if args.max_codes and args.max_codes > 0:
        tru_codes = tru_codes[: args.max_codes]

    all_rows: List[Dict[str, str]] = []
    failed_codes: List[TruCode] = []
    total = len(tru_codes)
    started_at = time.time()
    print(f"Loaded TRU codes: {total}")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                collect_for_code,
                tru_code=code,
                year=args.year,
                amount_from=amount_from,
                status=status,
                count_record=args.count_record,
                max_pages_per_code=max_pages_per_code,
            ): code
            for code in tru_codes
        }

        for idx, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows, total_records = future.result()
            except Exception as exc:  # pragma: no cover
                print(f"[{idx}/{total}] {code.code} FAILED: {exc}")
                failed_codes.append(code)
                continue
            all_rows.extend(rows)
            print(
                f"[{idx}/{total}] {code.code}: parsed={len(rows)} listed_total={total_records} "
                f"aggregate_rows={len(all_rows)}"
            )

    if failed_codes:
        print()
        print(f"Retrying failed codes sequentially: {len(failed_codes)}")
        retry_failed: List[TruCode] = []
        for idx, code in enumerate(failed_codes, start=1):
            try:
                rows, total_records = collect_for_code(
                    tru_code=code,
                    year=args.year,
                    amount_from=amount_from,
                    status=status,
                    count_record=args.count_record,
                    max_pages_per_code=max_pages_per_code,
                )
                all_rows.extend(rows)
                print(
                    f"[retry {idx}/{len(failed_codes)}] {code.code}: parsed={len(rows)} "
                    f"listed_total={total_records} aggregate_rows={len(all_rows)}"
                )
            except Exception as exc:  # pragma: no cover
                print(f"[retry {idx}/{len(failed_codes)}] {code.code} FAILED: {exc}")
                retry_failed.append(code)

        failed_codes = retry_failed

    unique = unique_rows(all_rows)
    unique.sort(key=lambda r: (r["Код ТРУ"], r["№ лота"]))
    output_path = Path(args.output)
    write_csv(output_path, unique)

    elapsed = time.time() - started_at
    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"Rows parsed: {len(all_rows)}")
    print(f"Rows unique: {len(unique)}")
    if failed_codes:
        failed_path = output_path.parent / "failed_codes.txt"
        write_failed_codes(failed_path, failed_codes)
        print(f"Failed codes: {len(failed_codes)} (saved to {failed_path.resolve()})")
    else:
        print("Failed codes: 0")
    print(f"Output: {output_path.resolve()}")


if __name__ == "__main__":
    main()
