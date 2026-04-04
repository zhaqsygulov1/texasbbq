#!/usr/bin/env python3
import argparse
import csv
import html
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"

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
class TruCode:
    code: str
    name: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value


def decode_gviz_csv(response: requests.Response) -> str:
    # gviz CSV is usually UTF-8, but in rare cases requests guesses cp1252.
    # Try UTF-8 first for stable Cyrillic parsing.
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.text


def fetch_with_retries(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 90,
    retries: int = 6,
    base_sleep: float = 1.0,
) -> requests.Response:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == retries:
                break
            sleep_for = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(
                f"[warn] request failed (attempt {attempt}/{retries}) "
                f"for {url}: {exc}; retrying in {sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
    raise RuntimeError(f"failed to fetch URL after retries: {url}") from last_exc


def load_tru_codes(session: requests.Session, sheet_id: str) -> list[TruCode]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    response = fetch_with_retries(session, url, timeout=180)
    text = decode_gviz_csv(response)
    rows = csv.reader(io.StringIO(text))
    header = next(rows, None)
    if not header:
        raise RuntimeError("source sheet is empty")

    result: list[TruCode] = []
    for row in rows:
        if not row:
            continue
        code = normalize_text(row[0] if len(row) > 0 else "")
        name = normalize_text(row[1] if len(row) > 1 else "")
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    return result


def load_existing_target_codes(session: requests.Session, sheet_id: str) -> set[str]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    response = fetch_with_retries(session, url, timeout=240)
    text = decode_gviz_csv(response)
    rows = csv.reader(io.StringIO(text))
    next(rows, None)  # header
    codes: set[str] = set()
    for row in rows:
        if len(row) > 1:
            code = normalize_text(row[1])
            if code:
                codes.add(code)
    return codes


def build_lots_url(code: str, year: int, amount_from: int, page: int, count_record: int) -> str:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[status][0]": "360",
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
        "page": str(page),
    }
    return f"https://goszakup.gov.kz/ru/search/lots?{urlencode(params)}"


def parse_total_pages(html_text: str) -> int:
    pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", html_text)]
    return max([1, *pages])


def parse_lot_rows(html_text: str, code: str) -> list[list[str]]:
    # HTML on goszakup often has malformed <td> closing tags.
    # html5lib rebuilds a consistent DOM and prevents column shifts.
    soup = BeautifulSoup(html_text, "html5lib")
    table = soup.find("table", attrs={"id": "search-result"})
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows_out: list[list[str]] = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = normalize_text(tds[0].get_text(" ", strip=True))
        if not lot_number or not re.search(r"\d{4,}-[A-ZА-ЯЁ0-9]+", lot_number):
            continue

        anno_strong = tds[1].find("strong")
        announcement = normalize_text(anno_strong.get_text(" ", strip=True) if anno_strong else tds[1].get_text(" ", strip=True))

        item_strong = tds[2].find("strong")
        item_name = normalize_text(item_strong.get_text(" ", strip=True) if item_strong else tds[2].get_text(" ", strip=True))
        lot_text = normalize_text(tds[2].get_text(" ", strip=True)).replace("История", "").strip()
        lot_text = normalize_text(lot_text) if lot_text else item_name

        qty = normalize_text(tds[3].get_text(" ", strip=True))
        amount = normalize_text(tds[4].get_text(" ", strip=True))
        method = normalize_text(tds[5].get_text(" ", strip=True))
        status = normalize_text(tds[6].get_text(" ", strip=True))

        rows_out.append(
            [
                lot_number,
                code,
                item_name,
                announcement,
                lot_text,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows_out


def append_csv_rows(output_path: Path, rows: Iterable[list[str]]) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = output_path.exists()
    written = 0
    with output_path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        if not file_exists:
            writer.writerow(OUTPUT_COLUMNS)
        for row in rows:
            writer.writerow(row)
            written += 1
    return written


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {
            "created_at": now_iso(),
            "last_index": 0,
            "processed_codes": 0,
            "rows_written": 0,
            "errors": [],
        }
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect 2025 lots by TRU codes from goszakup.")
    parser.add_argument("--output", default="out/lots_2025_collected.csv", help="Output CSV path")
    parser.add_argument("--state", default="out/collect_state.json", help="State JSON path")
    parser.add_argument("--year", type=int, default=2025, help="Filter year")
    parser.add_argument("--amount-from", type=int, default=15000000, help="Minimum amount filter")
    parser.add_argument("--count-record", type=int, default=2000, help="Records per page request")
    parser.add_argument("--max-codes-per-run", type=int, default=300, help="Stop after processing N codes in this run")
    parser.add_argument("--request-sleep", type=float, default=0.2, help="Sleep between code requests in seconds")
    parser.add_argument(
        "--only-codes",
        default="",
        help="Comma-separated list of TRU codes to process (for targeted runs)",
    )
    parser.add_argument(
        "--skip-codes-already-in-target",
        action="store_true",
        help="Skip TRU codes already present in target summary sheet",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    state_path = Path(args.state)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
    )

    print("[info] loading TRU codes from source sheet...", flush=True)
    tru_codes = load_tru_codes(session, SOURCE_SHEET_ID)
    if args.only_codes.strip():
        only = {normalize_text(x) for x in args.only_codes.split(",") if normalize_text(x)}
        tru_codes = [x for x in tru_codes if x.code in only]
        print(f"[info] filtered by --only-codes; {len(tru_codes)} codes left", flush=True)
    print(f"[info] loaded {len(tru_codes)} TRU codes", flush=True)

    target_codes: set[str] = set()
    if args.skip_codes_already_in_target:
        print("[info] loading existing codes from target sheet...", flush=True)
        target_codes = load_existing_target_codes(session, TARGET_SHEET_ID)
        print(f"[info] target sheet has {len(target_codes)} unique TRU codes", flush=True)

    state = load_state(state_path)
    last_index = int(state.get("last_index", 0))
    rows_written = int(state.get("rows_written", 0))
    processed_codes = int(state.get("processed_codes", 0))

    if last_index >= len(tru_codes):
        print("[info] all TRU codes already processed", flush=True)
        return 0

    start_ts = time.time()
    run_processed = 0

    print(
        f"[info] starting from index {last_index + 1}/{len(tru_codes)}; "
        f"max this run: {args.max_codes_per_run}",
        flush=True,
    )

    for idx in range(last_index, len(tru_codes)):
        if run_processed >= args.max_codes_per_run:
            break

        entry = tru_codes[idx]
        run_processed += 1
        processed_codes += 1

        if entry.code in target_codes:
            state["last_index"] = idx + 1
            state["processed_codes"] = processed_codes
            state["rows_written"] = rows_written
            state["updated_at"] = now_iso()
            save_state(state_path, state)
            print(
                f"[skip] {idx + 1}/{len(tru_codes)} code={entry.code} already in target sheet",
                flush=True,
            )
            continue

        code_rows: list[list[str]] = []
        seen: set[tuple[str, str, str, str]] = set()

        try:
            first_url = build_lots_url(
                entry.code,
                year=args.year,
                amount_from=args.amount_from,
                page=1,
                count_record=args.count_record,
            )
            first_response = fetch_with_retries(session, first_url)
            max_page = parse_total_pages(first_response.text)
            page_rows = parse_lot_rows(first_response.text, entry.code)
            for row in page_rows:
                key = (row[0], row[1], row[3], row[4])
                if key not in seen:
                    code_rows.append(row)
                    seen.add(key)

            if max_page > 1:
                for page in range(2, max_page + 1):
                    url = build_lots_url(
                        entry.code,
                        year=args.year,
                        amount_from=args.amount_from,
                        page=page,
                        count_record=args.count_record,
                    )
                    response = fetch_with_retries(session, url)
                    page_rows = parse_lot_rows(response.text, entry.code)
                    for row in page_rows:
                        key = (row[0], row[1], row[3], row[4])
                        if key not in seen:
                            code_rows.append(row)
                            seen.add(key)

            written_now = append_csv_rows(output_path, code_rows)
            rows_written += written_now

            print(
                f"[ok] {idx + 1}/{len(tru_codes)} code={entry.code} pages={max_page} rows={written_now}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            err = {
                "index": idx,
                "code": entry.code,
                "error": str(exc),
                "at": now_iso(),
            }
            errors = state.get("errors", [])
            errors.append(err)
            state["errors"] = errors[-200:]  # keep last 200 only
            print(f"[error] {idx + 1}/{len(tru_codes)} code={entry.code}: {exc}", flush=True)

        state["last_index"] = idx + 1
        state["processed_codes"] = processed_codes
        state["rows_written"] = rows_written
        state["updated_at"] = now_iso()
        save_state(state_path, state)
        time.sleep(args.request_sleep)

    elapsed = time.time() - start_ts
    print(
        f"[summary] processed_this_run={run_processed}, "
        f"processed_total={processed_codes}, rows_written_total={rows_written}, "
        f"next_index={state.get('last_index', 0)}, elapsed_sec={elapsed:.1f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
