#!/usr/bin/env python3
import argparse
import csv
import io
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_CODES_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/gviz/tq?tqx=out:csv&gid=0"
)
TARGET_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/export?format=csv&gid=0"
)
LOTS_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

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


def make_session() -> requests.Session:
    session = requests.Session()
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


def fetch_text_with_retries(
    session: requests.Session, url: str, *, params: Optional[dict] = None, retries: int = 6
) -> str:
    backoff = 2.0
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=90)
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception:
            if attempt == retries:
                raise
            time.sleep(backoff)
            backoff *= 1.8
    raise RuntimeError("Unexpected retry loop exit")


def parse_csv_text(text: str) -> List[List[str]]:
    return list(csv.reader(io.StringIO(text)))


def load_source_codes(session: requests.Session) -> Dict[str, str]:
    rows = parse_csv_text(fetch_text_with_retries(session, SOURCE_CODES_URL))
    if not rows:
        raise RuntimeError("Source codes sheet is empty")

    header = [h.strip() for h in rows[0]]
    if len(header) < 2 or header[0] != "Код ТРУ":
        raise RuntimeError(f"Unexpected source header: {header}")

    result: Dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        code = (row[0] if len(row) > 0 else "").strip()
        name = (row[1] if len(row) > 1 else "").strip()
        if code:
            result[code] = name
    return result


def load_target_existing(
    session: requests.Session,
) -> Tuple[List[List[str]], Set[Tuple[str, str]], Set[str]]:
    rows = parse_csv_text(fetch_text_with_retries(session, TARGET_EXPORT_URL))
    if not rows:
        raise RuntimeError("Target sheet is empty")
    header = rows[0]
    if header[: len(OUTPUT_HEADER)] != OUTPUT_HEADER:
        raise RuntimeError(f"Unexpected target header: {header[:len(OUTPUT_HEADER)]}")

    existing_rows: List[List[str]] = []
    existing_keys: Set[Tuple[str, str]] = set()
    existing_codes: Set[str] = set()
    for row in rows[1:]:
        if len(row) < len(OUTPUT_HEADER):
            continue
        lot_no = row[0].strip()
        code = row[1].strip()
        if not lot_no or not code:
            continue
        existing_rows.append(row[: len(OUTPUT_HEADER)])
        existing_keys.add((lot_no, code))
        existing_codes.add(code)
    return existing_rows, existing_keys, existing_codes


def clean_cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_total_records(soup: BeautifulSoup) -> int:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей", text)
    if not match:
        return 0
    value = match.group(1).replace(" ", "")
    return int(value) if value.isdigit() else 0


def get_lot_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [clean_cell(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        if headers and headers[0] == "№ лота":
            return table
    return None


def parse_lot_rows_from_soup(
    soup: BeautifulSoup,
    code: str,
    product_name: str,
) -> List[List[str]]:
    table = get_lot_table(soup)
    if table is None:
        return []

    rows_out: List[List[str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.select("td")
        if len(tds) < 7:
            continue

        lot_no = clean_cell(tds[0].get_text(" ", strip=True))

        announce_link = tds[1].select_one("a")
        announce_name = clean_cell(
            announce_link.get_text(" ", strip=True) if announce_link else tds[1].get_text(" ", strip=True)
        )

        lot_links = tds[2].select("a")
        lot_name = ""
        for link in lot_links:
            txt = clean_cell(link.get_text(" ", strip=True))
            if txt and txt != "История":
                lot_name = txt
                break
        if not lot_name:
            raw = clean_cell(tds[2].get_text(" ", strip=True))
            lot_name = clean_cell(raw.replace("История", ""))

        qty = clean_cell(tds[3].get_text(" ", strip=True))
        amount = clean_cell(tds[4].get_text(" ", strip=True))
        method = clean_cell(tds[5].get_text(" ", strip=True))
        status = clean_cell(tds[6].get_text(" ", strip=True))

        rows_out.append(
            [
                lot_no,
                code,
                product_name,
                announce_name,
                lot_name,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows_out


def fetch_code_rows(
    session: requests.Session, code: str, product_name: str, count_record: int
) -> List[List[str]]:
    base_params = {
        "filter[enstru]": code,
        "filter[status][]": "360",
        "filter[amount_from]": "15000000",
        "filter[year]": "2025",
        "count_record": str(count_record),
        "smb": "",
    }

    html = fetch_text_with_retries(session, LOTS_SEARCH_URL, params=base_params)
    soup = BeautifulSoup(html, "lxml")
    total = parse_total_records(soup)
    first_rows = parse_lot_rows_from_soup(soup, code, product_name)
    if total <= count_record:
        return first_rows

    rows: List[List[str]] = []
    rows.extend(first_rows)
    total_pages = int(math.ceil(total / count_record))
    for page in range(2, total_pages + 1):
        params = dict(base_params)
        params["page"] = str(page)
        page_html = fetch_text_with_retries(session, LOTS_SEARCH_URL, params=params)
        page_soup = BeautifulSoup(page_html, "lxml")
        rows.extend(parse_lot_rows_from_soup(page_soup, code, product_name))
        time.sleep(0.05)
    return rows


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_csv_rows(path: Path, rows: Sequence[Sequence[str]], write_header: bool) -> None:
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(OUTPUT_HEADER)
        writer.writerows(rows)


def worker_task(code: str, product_name: str, count_record: int) -> Tuple[str, List[List[str]], Optional[str]]:
    session = make_session()
    try:
        rows = fetch_code_rows(session, code, product_name, count_record)
        return code, rows, None
    except Exception as exc:
        return code, [], str(exc)
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect 2025 goszakup lots by TRU codes "
            "(status=360, amount_from=15000000) and build CSV output."
        )
    )
    parser.add_argument(
        "--output-new",
        default="lots_2025_new_rows.csv",
        help="File to store newly collected rows only.",
    )
    parser.add_argument(
        "--output-full",
        default="lots_2025_full.csv",
        help="File to store full dataset (existing target + new rows).",
    )
    parser.add_argument(
        "--progress-file",
        default="lots_2025_progress.json",
        help="JSON progress checkpoint for resumable runs.",
    )
    parser.add_argument(
        "--errors-file",
        default="lots_2025_errors.csv",
        help="CSV file with failed code fetches.",
    )
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers.")
    parser.add_argument(
        "--count-record",
        type=int,
        default=2000,
        help="Rows per page for goszakup (UI max is 2000).",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=0,
        help="Optional limit for debugging (0 = all selected codes).",
    )
    parser.add_argument(
        "--all-codes",
        action="store_true",
        help="Process all source codes. By default only codes missing in target are processed.",
    )
    args = parser.parse_args()

    output_new = Path(args.output_new)
    output_full = Path(args.output_full)
    progress_file = Path(args.progress_file)
    errors_file = Path(args.errors_file)

    main_session = make_session()
    source_map = load_source_codes(main_session)
    existing_rows, existing_keys, existing_codes = load_target_existing(main_session)
    main_session.close()

    source_codes = list(source_map.keys())
    if args.all_codes:
        base_codes = source_codes
    else:
        base_codes = [code for code in source_codes if code not in existing_codes]

    progress = load_json(progress_file, {"processed": [], "new_rows": 0, "errors": 0})
    processed_set = set(progress.get("processed", []))
    codes_to_process = [code for code in base_codes if code not in processed_set]

    if args.max_codes and args.max_codes > 0:
        codes_to_process = codes_to_process[: args.max_codes]

    print(f"Source codes: {len(source_codes)}")
    print(f"Existing target rows: {len(existing_rows)}")
    print(f"Existing target codes: {len(existing_codes)}")
    print(f"Codes selected for processing: {len(base_codes)}")
    print(f"Codes remaining after checkpoint: {len(codes_to_process)}")

    if not codes_to_process:
        print("Nothing to process. Building full output from existing target rows.")
        with output_full.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OUTPUT_HEADER)
            writer.writerows(existing_rows)
        return 0

    # Ensure output files are clean on first run only.
    if not output_new.exists() or output_new.stat().st_size == 0:
        append_csv_rows(output_new, [], write_header=True)
    if not errors_file.exists() or errors_file.stat().st_size == 0:
        with errors_file.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Код ТРУ", "Ошибка"])

    new_keys_in_run: Set[Tuple[str, str]] = set()
    total_new_rows = int(progress.get("new_rows", 0))
    total_errors = int(progress.get("errors", 0))

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(worker_task, code, source_map.get(code, ""), args.count_record): code
            for code in codes_to_process
        }
        done_counter = 0

        for future in as_completed(future_map):
            code = future_map[future]
            done_counter += 1
            try:
                code_res, rows, err = future.result()
            except Exception as exc:
                code_res, rows, err = code, [], str(exc)

            if err:
                total_errors += 1
                with errors_file.open("a", encoding="utf-8", newline="") as f:
                    csv.writer(f).writerow([code_res, err])
                progress["errors"] = total_errors
                print(f"[{done_counter}/{len(codes_to_process)}] {code_res}: ERROR {err}")
            else:
                dedup_rows: List[List[str]] = []
                for row in rows:
                    key = (row[0].strip(), row[1].strip())
                    if not key[0] or not key[1]:
                        continue
                    if key in existing_keys or key in new_keys_in_run:
                        continue
                    new_keys_in_run.add(key)
                    dedup_rows.append(row)

                if dedup_rows:
                    append_csv_rows(output_new, dedup_rows, write_header=False)
                    total_new_rows += len(dedup_rows)

                print(
                    f"[{done_counter}/{len(codes_to_process)}] {code_res}: "
                    f"{len(rows)} rows, {len(dedup_rows)} new"
                )

            processed_set.add(code_res)
            progress["processed"] = sorted(processed_set)
            progress["new_rows"] = total_new_rows
            save_json(progress_file, progress)

    # Build full CSV = existing target + new rows from this run.
    new_rows: List[List[str]] = []
    with output_new.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != OUTPUT_HEADER:
            raise RuntimeError(f"Unexpected header in {output_new}: {header}")
        for row in reader:
            if len(row) >= len(OUTPUT_HEADER):
                new_rows.append(row[: len(OUTPUT_HEADER)])

    full_rows = existing_rows + new_rows
    with output_full.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_HEADER)
        writer.writerows(full_rows)

    print("Done.")
    print(f"New unique rows in this run: {len(new_rows)}")
    print(f"Total rows in full output: {len(full_rows)}")
    print(f"Errors: {total_errors}")
    print(f"New rows file: {output_new}")
    print(f"Full rows file: {output_full}")
    print(f"Progress file: {progress_file}")
    print(f"Errors file: {errors_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
