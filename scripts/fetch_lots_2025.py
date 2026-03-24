#!/usr/bin/env python3
import argparse
import csv
import json
import math
import re
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
OUTPUT_HEADER = [
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


def clean_text(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def load_tru_codes() -> List[Tuple[str, str]]:
    response = requests.get(SOURCE_SHEET_CSV, timeout=120)
    response.raise_for_status()
    decoded = response.content.decode("utf-8-sig", errors="ignore")
    rows = list(csv.reader(decoded.splitlines()))
    result: List[Tuple[str, str]] = []
    for row in rows[1:]:
        if not row:
            continue
        code = clean_text(row[0]) if len(row) >= 1 else ""
        name = clean_text(row[1]) if len(row) >= 2 else ""
        if code:
            result.append((code, name))
    return result


def load_existing_state(existing_csv: Path) -> Tuple[Set[str], Set[str]]:
    lot_numbers: Set[str] = set()
    existing_codes: Set[str] = set()
    if not existing_csv.exists():
        return lot_numbers, existing_codes

    with existing_csv.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        try:
            next(reader)
        except StopIteration:
            return lot_numbers, existing_codes

        for row in reader:
            if len(row) < 2:
                continue
            lot = clean_text(row[0])
            code = clean_text(row[1])
            if lot:
                lot_numbers.add(lot)
            if code:
                existing_codes.add(code)

    return lot_numbers, existing_codes


def parse_total_records(soup: BeautifulSoup) -> int:
    info = soup.select_one(".dataTables_info strong")
    if not info:
        return 0
    text = clean_text(info.get_text(" ", strip=True))
    match = re.search(r"из\s+([\d\s]+)\s+запис", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_lot_rows(soup: BeautifulSoup, code: str, product_name: str) -> List[List[str]]:
    table_body = soup.select_one("#search-result tbody")
    if not table_body:
        return []

    rows: List[List[str]] = []
    for tr in table_body.select("tr"):
        cols = tr.find_all("td")
        if len(cols) < 7:
            continue

        lot_number = clean_text(cols[0].get_text(" ", strip=True))
        if not lot_number:
            continue

        announce_strong = cols[1].find("strong")
        announce_name = clean_text(
            announce_strong.get_text(" ", strip=True)
            if announce_strong
            else cols[1].get_text(" ", strip=True)
        )

        lot_anchor = cols[2].find("a")
        lot_title = clean_text(
            lot_anchor.get_text(" ", strip=True) if lot_anchor else cols[2].get_text(" ", strip=True)
        )
        extra_pieces: List[str] = []
        for small in cols[2].find_all("small"):
            small_text = clean_text(small.get_text(" ", strip=True))
            if small_text and small_text.lower() != "история":
                extra_pieces.append(small_text)
        if extra_pieces:
            lot_name_desc = clean_text(" ".join([lot_title] + extra_pieces))
        else:
            lot_name_desc = lot_title

        qty = clean_text(cols[3].get_text(" ", strip=True))
        amount = clean_text(cols[4].get_text(" ", strip=True))
        method = clean_text(cols[5].get_text(" ", strip=True))
        status = clean_text(cols[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_number,
                code,
                product_name,
                announce_name,
                lot_name_desc,
                qty,
                amount,
                method,
                status,
            ]
        )

    return rows


def build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    return session


def set_count_record(session: requests.Session, count_record: int) -> None:
    # First call initializes cookies for current session.
    session.get(SEARCH_URL, timeout=60)
    session.post(
        f"{SEARCH_URL}?ajax=Y&count_record={count_record}",
        data=str(count_record),
        timeout=60,
    )


def fetch_code_rows(
    session: requests.Session,
    code: str,
    product_name: str,
    year: int,
    status: str,
    amount_from: str,
    count_record: int,
    retries: int = 3,
) -> Tuple[List[List[str]], int]:
    params = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "filter[status][]": status,
        "filter[amount_from]": amount_from,
    }

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=120)
            response.raise_for_status()
            # goszakup pages contain invalid nesting in table rows; html5lib
            # normalizes DOM and prevents first-column text from being merged.
            soup = BeautifulSoup(response.text, "html5lib")
            total_records = parse_total_records(soup)
            rows = parse_lot_rows(soup, code, product_name)

            if total_records == 0:
                return [], 0

            # Portal displays up to 10 000 rows for one search response.
            pages = max(1, math.ceil(min(total_records, 10000) / count_record))
            for page in range(2, pages + 1):
                page_params = dict(params)
                page_params["page"] = str(page)
                page_response = session.get(SEARCH_URL, params=page_params, timeout=120)
                page_response.raise_for_status()
                page_soup = BeautifulSoup(page_response.text, "html5lib")
                rows.extend(parse_lot_rows(page_soup, code, product_name))
                time.sleep(0.05)

            return rows, total_records
        except Exception as error:  # noqa: BLE001
            last_error = error
            backoff = 2 ** attempt
            time.sleep(backoff)

    raise RuntimeError(f"Failed code {code}: {last_error}") from last_error


def load_progress(progress_path: Path) -> Dict:
    if not progress_path.exists():
        return {}
    return json.loads(progress_path.read_text(encoding="utf-8"))


def save_progress(progress_path: Path, payload: Dict) -> None:
    progress_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_output_csv(output_path: Path) -> None:
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(OUTPUT_HEADER)


def append_rows(output_path: Path, rows: List[List[str]]) -> None:
    if not rows:
        return
    with output_path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", default="360")
    parser.add_argument("--amount-from", default="15000000")
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--process-mode", choices=["missing", "all"], default="missing")
    parser.add_argument(
        "--existing-csv",
        default="data/target_existing.csv",
        help="Current target sheet CSV used for dedupe and missing-code detection.",
    )
    parser.add_argument("--output-csv", default="data/new_rows_2025.csv")
    parser.add_argument("--progress-json", default="data/fetch_progress_2025.json")
    args = parser.parse_args()

    existing_csv = Path(args.existing_csv)
    output_csv = Path(args.output_csv)
    progress_json = Path(args.progress_json)

    print("Loading source TRU codes...")
    codes = load_tru_codes()
    print(f"Loaded codes: {len(codes)}")

    existing_lot_numbers, existing_codes = load_existing_state(existing_csv)
    print(
        f"Existing state: lots={len(existing_lot_numbers)}, codes={len(existing_codes)} "
        f"from {existing_csv}"
    )

    if args.process_mode == "missing":
        target_codes = [(c, n) for c, n in codes if c not in existing_codes]
    else:
        target_codes = codes

    if args.max_codes > 0:
        target_codes = target_codes[: args.max_codes]

    print(f"Codes to process: {len(target_codes)}")
    if not target_codes:
        print("Nothing to process.")
        return

    progress = load_progress(progress_json) if args.resume else {}
    next_index = int(progress.get("next_index", 0)) if progress else 0
    if next_index >= len(target_codes):
        print("Progress indicates all selected codes are already processed.")
        return

    ensure_output_csv(output_csv)

    session = build_session(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    set_count_record(session, args.count_record)

    new_lot_numbers: Set[str] = set()
    written_rows = int(progress.get("written_rows", 0)) if progress else 0
    processed_codes = next_index
    non_empty_codes = int(progress.get("non_empty_codes", 0)) if progress else 0
    errors: List[Dict[str, str]] = progress.get("errors", []) if progress else []

    start_time = time.time()
    for idx in range(next_index, len(target_codes)):
        code, product_name = target_codes[idx]
        try:
            rows, total = fetch_code_rows(
                session=session,
                code=code,
                product_name=product_name,
                year=args.year,
                status=args.status,
                amount_from=args.amount_from,
                count_record=args.count_record,
            )

            unique_rows: List[List[str]] = []
            for row in rows:
                lot_no = row[0]
                if lot_no in existing_lot_numbers or lot_no in new_lot_numbers:
                    continue
                unique_rows.append(row)
                new_lot_numbers.add(lot_no)

            if unique_rows:
                append_rows(output_csv, unique_rows)
                written_rows += len(unique_rows)
                non_empty_codes += 1

            processed_codes += 1
            elapsed = time.time() - start_time
            print(
                f"[{processed_codes}/{len(target_codes)}] {code} "
                f"total={total} fetched={len(rows)} new={len(unique_rows)} "
                f"written={written_rows} elapsed={elapsed:.1f}s"
            )
        except Exception as error:  # noqa: BLE001
            message = str(error)
            errors.append({"code": code, "error": message})
            processed_codes += 1
            print(f"[{processed_codes}/{len(target_codes)}] ERROR {code}: {message}")

        save_progress(
            progress_json,
            {
                "next_index": idx + 1,
                "written_rows": written_rows,
                "non_empty_codes": non_empty_codes,
                "processed_codes": processed_codes,
                "total_codes": len(target_codes),
                "errors": errors[-100:],
                "updated_at_epoch": int(time.time()),
                "process_mode": args.process_mode,
            },
        )
        time.sleep(0.05)

    print("Done.")
    print(f"Processed codes: {processed_codes}/{len(target_codes)}")
    print(f"Codes with new rows: {non_empty_codes}")
    print(f"New unique rows written: {written_rows}")
    print(f"Progress file: {progress_json}")
    print(f"Output CSV: {output_csv}")


if __name__ == "__main__":
    main()
