#!/usr/bin/env python3
import argparse
import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"

SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/gviz/tq?tqx=out:csv"
)
TARGET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/export?format=csv"
)
LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"
CAP_RECORDS_PER_QUERY = 10000
PAGE_SIZE = 2000
MONTH_VALUES = [str(m) for m in range(1, 13)] + ["99"]
CSV_HEADER = [
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


def fetch_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
    max_attempts: int = 6,
    timeout: int = 45,
) -> requests.Response:
    delay = 1.5
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt == max_attempts:
                break
            time.sleep(delay)
            delay = min(delay * 1.7, 20.0)
    raise RuntimeError(f"Failed request to {url} with params={params!r}: {last_err}")  # noqa: TRY003


def parse_total_records(soup: BeautifulSoup) -> int:
    info = soup.select_one("div.dataTables_info strong")
    if not info:
        return 0
    text = info.get_text(" ", strip=True)
    match = re.search(r"из\s+([\d\s]+)\s+запис", text)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_rows_from_page(soup: BeautifulSoup) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in soup.select("table#search-result tbody tr"):
        tds = tr.select("td")
        if len(tds) < 7:
            continue
        lot_no = tds[0].get_text(" ", strip=True)
        announce_el = tds[1].select_one("a strong")
        lot_name_el = tds[2].select_one("a strong")
        announce_name = announce_el.get_text(" ", strip=True) if announce_el else ""
        lot_name = lot_name_el.get_text(" ", strip=True) if lot_name_el else ""
        qty = tds[3].get_text(" ", strip=True)
        amount = tds[4].get_text(" ", strip=True)
        buy_method = tds[5].get_text(" ", strip=True)
        status = tds[6].get_text(" ", strip=True)
        if not lot_no:
            continue
        rows.append([lot_no, announce_name, lot_name, qty, amount, buy_method, status])
    return rows


def request_page(
    session: requests.Session,
    code: str,
    *,
    page: int,
    month: Optional[str],
    year: str,
    status_filters: Sequence[str],
    amount_from: Optional[str],
    amount_to: Optional[str],
) -> Tuple[int, List[List[str]]]:
    params = {
        "filter[enstru]": code,
        "filter[year]": year,
        "count_record": str(PAGE_SIZE),
        "page": str(page),
    }
    if status_filters:
        params["filter[status][]"] = list(status_filters)
    if amount_from:
        params["filter[amount_from]"] = amount_from
    if amount_to:
        params["filter[amount_to]"] = amount_to
    if month is not None:
        params["filter[month]"] = month
    response = fetch_with_retries(session, LOTS_URL, params=params)
    soup = BeautifulSoup(response.text, "lxml")
    total = parse_total_records(soup)
    return total, parse_rows_from_page(soup)


def iter_partitioned_rows(
    session: requests.Session,
    code: str,
    *,
    year: str,
    warning_sink: List[str],
    status_filters: Sequence[str],
    amount_from: Optional[str],
    amount_to: Optional[str],
) -> Iterable[List[str]]:
    base_total, first_rows = request_page(
        session,
        code,
        page=1,
        month=None,
        year=year,
        status_filters=status_filters,
        amount_from=amount_from,
        amount_to=amount_to,
    )
    if base_total == 0 and not first_rows:
        return

    if base_total < CAP_RECORDS_PER_QUERY:
        for row in first_rows:
            yield row
        total_pages = max(1, (base_total + PAGE_SIZE - 1) // PAGE_SIZE)
        for page in range(2, total_pages + 1):
            _, rows = request_page(
                session,
                code,
                page=page,
                month=None,
                year=year,
                status_filters=status_filters,
                amount_from=amount_from,
                amount_to=amount_to,
            )
            if not rows:
                break
            for row in rows:
                yield row
        return

    warning_sink.append(
        f"Code {code}: total reached cap {CAP_RECORDS_PER_QUERY}, splitting by month."
    )
    for month in MONTH_VALUES:
        month_total, rows = request_page(
            session,
            code,
            page=1,
            month=month,
            year=year,
            status_filters=status_filters,
            amount_from=amount_from,
            amount_to=amount_to,
        )
        for row in rows:
            yield row
        total_pages = max(1, (month_total + PAGE_SIZE - 1) // PAGE_SIZE)
        for page in range(2, total_pages + 1):
            _, page_rows = request_page(
                session,
                code,
                page=page,
                month=month,
                year=year,
                status_filters=status_filters,
                amount_from=amount_from,
                amount_to=amount_to,
            )
            if not page_rows:
                break
            for row in page_rows:
                yield row
        if month_total >= CAP_RECORDS_PER_QUERY:
            warning_sink.append(
                f"Code {code}: month={month} also reached cap {CAP_RECORDS_PER_QUERY}."
            )


def read_source_codes(session: requests.Session) -> List[Tuple[str, str]]:
    response = fetch_with_retries(session, SOURCE_CSV_URL, timeout=60)
    response.encoding = "utf-8"
    rows = csv.reader(io.StringIO(response.text))
    result: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        if idx == 0:
            continue
        if not row or not row[0].strip():
            continue
        code = row[0].strip()
        name = row[1].strip() if len(row) > 1 else ""
        if code in seen:
            continue
        seen.add(code)
        result.append((code, name))
    return result


def read_existing_pairs(
    session: requests.Session,
    *,
    skip_target_read: bool,
) -> set[Tuple[str, str]]:
    if skip_target_read:
        return set()
    response = fetch_with_retries(session, TARGET_CSV_URL, timeout=180)
    response.encoding = "utf-8"
    pairs: set[Tuple[str, str]] = set()
    for idx, row in enumerate(csv.reader(io.StringIO(response.text))):
        if idx == 0:
            continue
        if len(row) < 2:
            continue
        lot_no = row[0].strip()
        code = row[1].strip()
        if lot_no and code:
            pairs.add((lot_no, code))
    return pairs


def read_existing_codes(
    session: requests.Session,
    *,
    skip_target_read: bool,
) -> set[str]:
    if skip_target_read:
        return set()
    response = fetch_with_retries(session, TARGET_CSV_URL, timeout=180)
    response.encoding = "utf-8"
    codes: set[str] = set()
    for idx, row in enumerate(csv.reader(io.StringIO(response.text))):
        if idx == 0:
            continue
        if len(row) < 2:
            continue
        code = row[1].strip()
        if code:
            codes.add(code)
    return codes


def load_checkpoint(path: Path) -> Dict:
    if not path.exists():
        return {
            "next_index": 0,
            "processed_codes": 0,
            "written_rows": 0,
            "warnings": [],
            "started_at": int(time.time()),
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, state: Dict) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect lots by TRU codes for year 2025 from goszakup."
    )
    parser.add_argument("--year", default="2025")
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help=(
            "Lot status code. Repeatable argument. "
            "Default is 360 (Закупка состоялась)."
        ),
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Minimum lot amount filter. Default: 15000000.",
    )
    parser.add_argument(
        "--amount-to",
        default="",
        help="Maximum lot amount filter (optional).",
    )
    parser.add_argument(
        "--output-csv",
        default="output/lots_2025_new_rows.csv",
        help="Path to output CSV with newly parsed rows.",
    )
    parser.add_argument(
        "--checkpoint",
        default="output/lots_2025_checkpoint.json",
        help="Path to checkpoint JSON file.",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=0,
        help="If > 0, process only first N codes from current checkpoint.",
    )
    parser.add_argument(
        "--skip-target-read",
        action="store_true",
        help="Do not load existing rows from target Google Sheet.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Inclusive source-code index start after filtering.",
    )
    parser.add_argument(
        "--end-index",
        type=int,
        default=0,
        help="Exclusive source-code index end after filtering; 0 means all.",
    )
    parser.add_argument(
        "--only-missing-codes",
        action="store_true",
        help="Process only TRU codes that are absent in target sheet.",
    )
    args = parser.parse_args()

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = Path(args.checkpoint)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )

    print("Loading source TRU codes...")
    source_codes = read_source_codes(session)
    print(f"Loaded {len(source_codes)} unique source codes.")

    if not args.status:
        args.status = ["360"]

    if args.only_missing_codes and not args.skip_target_read:
        print("Loading existing target codes...")
        existing_codes = read_existing_codes(
            session, skip_target_read=args.skip_target_read
        )
        source_codes = [(c, n) for (c, n) in source_codes if c not in existing_codes]
        print(f"Codes absent in target: {len(source_codes)}")

    slice_start = max(0, args.start_index)
    slice_end = args.end_index if args.end_index > 0 else len(source_codes)
    if slice_end < slice_start:
        raise ValueError("--end-index must be greater than or equal to --start-index")
    source_codes = source_codes[slice_start:slice_end]
    print(
        f"Selected source index range [{slice_start}, {slice_start + len(source_codes)}) "
        f"-> {len(source_codes)} codes."
    )

    print("Loading existing target lot/code pairs...")
    known_pairs = read_existing_pairs(session, skip_target_read=args.skip_target_read)
    print(f"Known existing pairs: {len(known_pairs)}")

    state = load_checkpoint(checkpoint_path)
    state.pop("finished_at", None)
    start_idx = int(state.get("next_index", 0))
    processed_codes = int(state.get("processed_codes", 0))
    written_rows = int(state.get("written_rows", 0))
    warnings: List[str] = list(state.get("warnings", []))

    if start_idx >= len(source_codes):
        print("All codes already processed according to checkpoint.")
        return

    write_header = not output_path.exists() or output_path.stat().st_size == 0
    with output_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        max_codes = args.max_codes if args.max_codes > 0 else len(source_codes)
        stop_at = min(len(source_codes), start_idx + max_codes)
        for idx in range(start_idx, stop_at):
            code, product_name = source_codes[idx]
            code_new_rows = 0
            for raw_row in iter_partitioned_rows(
                session,
                code,
                year=args.year,
                warning_sink=warnings,
                status_filters=args.status,
                amount_from=args.amount_from.strip() or None,
                amount_to=args.amount_to.strip() or None,
            ):
                lot_no = raw_row[0]
                key = (lot_no, code)
                if key in known_pairs:
                    continue
                known_pairs.add(key)
                writer.writerow(
                    [
                        lot_no,
                        code,
                        product_name,
                        raw_row[1],
                        raw_row[2],
                        raw_row[3],
                        raw_row[4],
                        raw_row[5],
                        raw_row[6],
                    ]
                )
                code_new_rows += 1
                written_rows += 1

            processed_codes += 1
            state["next_index"] = idx + 1
            state["processed_codes"] = processed_codes
            state["written_rows"] = written_rows
            state["warnings"] = warnings[-500:]
            save_checkpoint(checkpoint_path, state)

            print(
                f"[{idx + 1}/{len(source_codes)}] code={code} "
                f"new_rows={code_new_rows} total_written={written_rows}"
            )

    state["finished_at"] = int(time.time())
    save_checkpoint(checkpoint_path, state)
    print(f"Done. Output CSV: {output_path}")
    print(f"Checkpoint: {checkpoint_path}")
    if warnings:
        print(f"Warnings count: {len(warnings)} (last 5 below)")
        for line in warnings[-5:]:
            print(" -", line)


if __name__ == "__main__":
    main()
