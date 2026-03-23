#!/usr/bin/env python3
"""
Build lot list for year 2025 by TRU codes from a Google Sheet.

Input sheet format:
  - Column A: Код ТРУ
  - Column B: Название

Output CSV columns:
  № лота, Код ТРУ, Наименование товара, Наименование объявления,
  Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEFAULT_YEAR = "2025"
DEFAULT_OUTPUT = "lots_2025_by_tru.csv"
DEFAULT_STATE = "lots_2025_by_tru.state.json"
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
TOTAL_RE = re.compile(
    r"Показано\s*c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей", re.IGNORECASE
)


thread_local = threading.local()


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        }
    )
    return session


def get_session() -> requests.Session:
    if not hasattr(thread_local, "session"):
        thread_local.session = make_session()
    return thread_local.session


def collapse_ws(value: str) -> str:
    return " ".join(value.split())


def get_first_link_text_bs4(cell) -> str:
    link = cell.select_one("a")
    if link:
        return collapse_ws(link.get_text(" ", strip=True))
    text = cell.get_text(" ", strip=True)
    text = text.replace("История", "")
    return collapse_ws(text)


def parse_total(html_text: str) -> int:
    match = TOTAL_RE.search(html_text)
    if not match:
        return 0
    return int(match.group(1).replace(" ", ""))


def parse_rows(html_text: str, code: str, item_name: str) -> list[list[str]]:
    soup = BeautifulSoup(html_text, "lxml")
    lot_table = None
    for table in soup.select("table"):
        headers = [th.get_text(" ", strip=True) for th in table.select("th")]
        if headers[:1] == ["№ лота"]:
            lot_table = table
            break

    if lot_table is None:
        return []

    rows = []
    for tr in lot_table.select("tr")[1:]:
        tds = tr.select("td")
        if len(tds) < 7:
            continue

        lot_no = collapse_ws(tds[0].get_text(" ", strip=True))
        announcement = get_first_link_text_bs4(tds[1])
        lot_description = get_first_link_text_bs4(tds[2])
        qty = collapse_ws(tds[3].get_text(" ", strip=True))
        amount = collapse_ws(tds[4].get_text(" ", strip=True))
        method = collapse_ws(tds[5].get_text(" ", strip=True))
        status = collapse_ws(tds[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_no,
                code,
                item_name,
                announcement,
                lot_description,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows


def fetch_page(
    code: str, year: str, page: int, count_record: int, max_attempts: int = 6
) -> str:
    session = get_session()
    params = {
        "filter[enstru]": code,
        "filter[year]": year,
        "count_record": str(count_record),
        "page": str(page),
    }
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(BASE_URL, params=params, timeout=90)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_s = min(20, 1.2 * attempt)
            time.sleep(sleep_s)
    raise RuntimeError(
        f"Failed to fetch code={code} page={page} after {max_attempts} attempts"
    ) from last_error


def fetch_code_lots(code: str, item_name: str, year: str, count_record: int) -> dict:
    first_html = fetch_page(code=code, year=year, page=1, count_record=count_record)
    total = parse_total(first_html)
    pages = max(1, math.ceil(total / count_record)) if total else 1

    all_rows = parse_rows(first_html, code=code, item_name=item_name)
    for page in range(2, pages + 1):
        html_text = fetch_page(code=code, year=year, page=page, count_record=count_record)
        all_rows.extend(parse_rows(html_text, code=code, item_name=item_name))

    return {
        "code": code,
        "item_name": item_name,
        "total_reported": total,
        "rows": all_rows,
        "pages": pages,
    }


def load_source_codes(sheet_id: str) -> list[tuple[str, str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
    text = requests.get(url, timeout=90).text
    parsed = list(csv.reader(io.StringIO(text)))
    rows = []
    for row in parsed[1:]:
        if not row:
            continue
        code = row[0].strip()
        if not code:
            continue
        item_name = row[1].strip() if len(row) > 1 else ""
        rows.append((code, item_name))
    return rows


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"done_codes": [], "stats": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_output_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
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
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sheet-id", default=DEFAULT_SOURCE_SHEET_ID)
    parser.add_argument("--year", default=DEFAULT_YEAR)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--limit-codes", type=int, default=0)
    return parser.parse_args()


def iter_pending(
    source_rows: list[tuple[str, str]], done_codes: set[str], limit_codes: int
) -> Iterable[tuple[str, str]]:
    pending = [(code, name) for code, name in source_rows if code not in done_codes]
    if limit_codes > 0:
        return pending[:limit_codes]
    return pending


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    state_path = Path(args.state)

    source_rows = load_source_codes(args.source_sheet_id)
    state = load_state(state_path)
    done_codes = set(state.get("done_codes", []))
    stats = state.get("stats", {})

    pending = list(iter_pending(source_rows, done_codes, args.limit_codes))
    print(f"Source codes: {len(source_rows)}")
    print(f"Already done: {len(done_codes)}")
    print(f"Pending now: {len(pending)}")
    print(
        f"Params: year={args.year}, workers={args.workers}, count_record={args.count_record}"
    )

    ensure_output_header(output_path)
    with output_path.open("a", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    fetch_code_lots,
                    code=code,
                    item_name=item_name,
                    year=args.year,
                    count_record=args.count_record,
                ): (code, item_name)
                for code, item_name in pending
            }

            completed = 0
            total_rows_written = 0
            for future in as_completed(future_map):
                code, _name = future_map[future]
                try:
                    result = future.result()
                    rows = result["rows"]
                    if rows:
                        writer.writerows(rows)
                        out_f.flush()
                    total_reported = result["total_reported"]
                    parsed_rows = len(rows)
                    done_codes.add(code)
                    stats[code] = {
                        "total_reported": total_reported,
                        "parsed_rows": parsed_rows,
                        "pages": result["pages"],
                        "ok": True,
                    }
                    total_rows_written += parsed_rows
                    status = "OK"
                except Exception as exc:  # noqa: BLE001
                    stats[code] = {"ok": False, "error": str(exc)}
                    status = f"ERR: {exc}"

                completed += 1
                state["done_codes"] = sorted(done_codes)
                state["stats"] = stats
                save_state(state_path, state)
                print(
                    f"[{completed}/{len(pending)}] {code}: {status} | "
                    f"rows_written_total={total_rows_written}"
                )

    print("Done.")
    print(f"Output: {output_path.resolve()}")
    print(f"State:  {state_path.resolve()}")


if __name__ == "__main__":
    main()
