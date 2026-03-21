#!/usr/bin/env python3
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_ID_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_CSV_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
GOSZAKUP_LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"

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

TOTAL_RE = re.compile(r"Показано c\s*(\d+)\s*по\s*(\d+)\s*из\s*([\d\s]+)\s*записей", re.I)
LOT_NUM_RE = re.compile(r"^(\d+-\S+)")


@dataclass
class TruItem:
    code: str
    name: str


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
    )
    return session


def request_text(session: requests.Session, url: str, params: Dict[str, str], timeout: int = 90) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, 7):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_s = min(20, 2**attempt)
            print(f"[WARN] Request failed (attempt {attempt}/6): {exc}. Sleep {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    raise RuntimeError(f"Request failed after retries: {url} params={params}") from last_error


def fetch_tru_items(session: requests.Session, source_sheet_id: str) -> List[TruItem]:
    url = SOURCE_CSV_URL_TEMPLATE.format(sheet_id=source_sheet_id)
    text = request_text(session, url, params={})
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise RuntimeError("Source sheet is empty")

    items: List[TruItem] = []
    for row in rows[1:]:
        if not row:
            continue
        code = (row[0] if len(row) > 0 else "").strip()
        name = (row[1] if len(row) > 1 else "").strip()
        if not code:
            continue
        items.append(TruItem(code=code, name=name))
    return items


def parse_total_records(html: str) -> int:
    match = TOTAL_RE.search(html)
    if not match:
        return 0
    total_raw = match.group(3).replace(" ", "")
    try:
        return int(total_raw)
    except ValueError:
        return 0


def first_non_history_link_text(cell) -> str:
    for link in cell.find_all("a"):
        text = " ".join(link.get_text(" ", strip=True).split())
        if text and text.lower() != "история":
            return text
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_lot_number(cell_text: str) -> str:
    clean = " ".join(cell_text.split())
    match = LOT_NUM_RE.match(clean)
    if match:
        return match.group(1)
    return clean.split(" ", 1)[0] if clean else ""


def parse_lots_from_html(html: str, tru_code: str, tru_name: str) -> List[List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    result_table = None
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if "№ лота" in headers and "Сумма, тг." in headers:
            result_table = table
            break

    if result_table is None:
        return []

    rows: List[List[str]] = []
    for tr in result_table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue

        first_cell_text = " ".join(cells[0].get_text(" ", strip=True).split())
        if not first_cell_text or "Записи отсутствуют" in first_cell_text:
            continue

        lot_number = parse_lot_number(first_cell_text)
        announcement_name = first_non_history_link_text(cells[1])
        lot_desc = first_non_history_link_text(cells[2])
        qty = " ".join(cells[3].get_text(" ", strip=True).split())
        amount = " ".join(cells[4].get_text(" ", strip=True).split())
        method = " ".join(cells[5].get_text(" ", strip=True).split())
        status = " ".join(cells[6].get_text(" ", strip=True).split())

        rows.append(
            [
                lot_number,
                tru_code,
                tru_name,
                announcement_name,
                lot_desc,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows


def build_search_params(
    tru_code: str,
    year: int,
    status_code: int,
    amount_from: int,
    count_record: int,
    page: int,
) -> Dict[str, str]:
    # Keeping full query structure close to the portal form improves compatibility.
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": tru_code,
        "filter[status][]": str(status_code),
        "filter[customer]": "",
        "filter[amount_from]": str(amount_from),
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
        "count_record": str(count_record),
    }
    if page > 1:
        params["page"] = str(page)
    return params


def load_state(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {"processed_codes": [], "rows_written": 0}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: Dict[str, object]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def open_output_csv(path: str) -> Tuple[io.TextIOBase, csv.writer]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    file_exists = os.path.exists(path) and os.path.getsize(path) > 0
    f = open(path, "a", encoding="utf-8-sig", newline="")
    writer = csv.writer(f)
    if not file_exists:
        writer.writerow(OUTPUT_HEADERS)
        f.flush()
    return f, writer


def unique_tru_items(items: Iterable[TruItem]) -> List[TruItem]:
    result: List[TruItem] = []
    seen: Set[str] = set()
    for item in items:
        if item.code in seen:
            continue
        seen.add(item.code)
        result.append(item)
    return result


def run(args: argparse.Namespace) -> int:
    session = build_session()

    print("[INFO] Loading source TРУ table...", flush=True)
    tru_items = unique_tru_items(fetch_tru_items(session, args.source_sheet_id))
    print(f"[INFO] Loaded {len(tru_items)} unique TРУ codes", flush=True)

    if args.max_codes > 0:
        tru_items = tru_items[: args.max_codes]
        print(f"[INFO] max-codes enabled => processing first {len(tru_items)} codes", flush=True)

    state = load_state(args.state_path)
    processed_codes: Set[str] = set(state.get("processed_codes", []))
    rows_written = int(state.get("rows_written", 0))
    print(f"[INFO] Resume state: processed={len(processed_codes)} rows_written={rows_written}", flush=True)

    out_file, out_writer = open_output_csv(args.output_csv)
    try:
        for idx, item in enumerate(tru_items, start=1):
            if item.code in processed_codes:
                continue

            print(f"[INFO] [{idx}/{len(tru_items)}] Code {item.code} — {item.name}", flush=True)
            page = 1
            page_count = 1
            seen_lot_numbers: Set[str] = set()
            code_rows = 0

            while page <= page_count:
                params = build_search_params(
                    tru_code=item.code,
                    year=args.year,
                    status_code=args.status_code,
                    amount_from=args.amount_from,
                    count_record=args.count_record,
                    page=page,
                )
                html = request_text(session, GOSZAKUP_LOTS_URL, params=params)

                if page == 1:
                    total_records = parse_total_records(html)
                    page_count = max(1, math.ceil(total_records / args.count_record))
                    print(
                        f"[INFO]   total={total_records}, pages={page_count}, page_size={args.count_record}",
                        flush=True,
                    )

                parsed_rows = parse_lots_from_html(html, item.code, item.name)
                page_unique_rows = 0
                for row in parsed_rows:
                    lot_number = row[0]
                    if not lot_number or lot_number in seen_lot_numbers:
                        continue
                    seen_lot_numbers.add(lot_number)
                    out_writer.writerow(row)
                    rows_written += 1
                    code_rows += 1
                    page_unique_rows += 1

                out_file.flush()
                print(
                    f"[INFO]   page {page}/{page_count}: parsed={len(parsed_rows)}, written={page_unique_rows}",
                    flush=True,
                )
                page += 1
                if args.page_delay > 0:
                    time.sleep(args.page_delay)

            processed_codes.add(item.code)
            state = {
                "processed_codes": sorted(processed_codes),
                "rows_written": rows_written,
                "last_code": item.code,
                "last_code_rows": code_rows,
                "updated_at_epoch": int(time.time()),
            }
            save_state(args.state_path, state)
            print(f"[INFO] Completed code {item.code}: wrote {code_rows} rows", flush=True)

            if args.code_delay > 0:
                time.sleep(args.code_delay)
    finally:
        out_file.close()

    print(f"[DONE] Finished. Total rows written: {rows_written}", flush=True)
    print(f"[DONE] Output CSV: {args.output_csv}", flush=True)
    print(f"[DONE] State file: {args.state_path}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect 2025 goszakup lots by TРУ codes from Google Sheet and save to CSV."
    )
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID_DEFAULT)
    parser.add_argument("--output-csv", default="/workspace/outputs/lots_2025_by_tru.csv")
    parser.add_argument("--state-path", default="/workspace/outputs/lots_2025_state.json")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status-code", type=int, default=360)
    parser.add_argument("--amount-from", type=int, default=15000000)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--page-delay", type=float, default=0.35)
    parser.add_argument("--code-delay", type=float, default=0.35)
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
