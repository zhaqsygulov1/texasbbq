#!/usr/bin/env python3
"""
Collect 2025 lots from goszakup.gov.kz for TRU codes from a Google Sheet.

Input sheet format:
  Код ТРУ | Название

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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GOOGLE_GVIZ_CSV = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"

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

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RE_MULTI_SPACE = re.compile(r"\s+")
RE_TOTAL = re.compile(r"из\s+([\d\s]+)\s+записей", re.IGNORECASE)
RE_PAGE = re.compile(r"[?&]page=(\d+)")
RE_HISTORY_TRAIL = re.compile(r"\s*История\s*$", re.IGNORECASE)
RE_POSSIBLE_LOT = re.compile(r"^\d{4,}-")
CAPTCHA_TITLE_MARKER = "429 Too Many Requests"


@dataclass(frozen=True)
class TruItem:
    code: str
    name: str


def normalize_text(value: str) -> str:
    return RE_MULTI_SPACE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def fetch_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
    retries: int = 4,
    timeout: int = 40,
) -> requests.Response:
    delay = 1.5
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 200:
                return response
            raise RuntimeError(f"HTTP {response.status_code} for {response.url}")
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt == retries:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Failed to fetch {url}: {last_err}") from last_err


def ensure_valid_lots_page(response: requests.Response, code: str, page: int) -> None:
    soup = BeautifulSoup(response.text, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if CAPTCHA_TITLE_MARKER.lower() in title.lower() or "/captcha" in response.url:
        raise RuntimeError(
            f"Rate limited / captcha for code={code}, page={page} ({response.url})"
        )

    table = soup.select_one("table tbody")
    info = soup.select_one("div.dataTables_info")
    if table is None or info is None:
        raise RuntimeError(
            f"Unexpected response structure for code={code}, page={page} ({response.url})"
        )


def fetch_lots_page(
    session: requests.Session,
    code: str,
    year: int,
    page: int,
    page_size: int,
    retries: int = 8,
) -> requests.Response:
    delay = 2.0
    last_err: Exception | None = None
    params = page_params(code, year, page=page, page_size=page_size)

    for attempt in range(1, retries + 1):
        try:
            response = fetch_with_retry(
                session, LOTS_URL, params=params, retries=3, timeout=45
            )
            ensure_valid_lots_page(response, code=code, page=page)
            return response
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt == retries:
                break
            time.sleep(delay)
            delay = min(delay * 2, 35.0)

    raise RuntimeError(
        f"Failed to fetch valid lots page for code={code}, page={page}: {last_err}"
    ) from last_err


def read_tru_items(sheet_id: str) -> list[TruItem]:
    session = get_session()
    url = GOOGLE_GVIZ_CSV.format(sheet_id=sheet_id)
    text = fetch_with_retry(session, url, retries=3, timeout=30).text
    reader = csv.reader(io.StringIO(text))

    result: list[TruItem] = []
    seen: set[str] = set()
    for idx, row in enumerate(reader):
        if idx == 0:
            continue
        if not row:
            continue
        code = normalize_text(row[0] if len(row) > 0 else "")
        name = normalize_text(row[1] if len(row) > 1 else "")
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append(TruItem(code=code, name=name))
    return result


def parse_total_records(soup: BeautifulSoup) -> int:
    info = soup.select_one("div.dataTables_info")
    if info:
        text = normalize_text(info.get_text(" ", strip=True))
        match = RE_TOTAL.search(text)
        if match:
            return int(match.group(1).replace(" ", ""))

    max_page = 1
    for anchor in soup.select("ul.pagination a[href]"):
        href = anchor.get("href") or ""
        match = RE_PAGE.search(href)
        if match:
            max_page = max(max_page, int(match.group(1)))
    return max_page * 50


def extract_lot_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tr in soup.select("table tbody tr"):
        tds = tr.select("td")
        if len(tds) != 7:
            continue
        values = [normalize_text(td.get_text(" ", strip=True)) for td in tds]
        lot_no = values[0]
        if not RE_POSSIBLE_LOT.match(lot_no):
            continue

        lot_text = RE_HISTORY_TRAIL.sub("", values[2]).strip()
        rows.append(
            {
                "lot_no": lot_no,
                "announcement": values[1],
                "lot_description": lot_text,
                "qty": values[3],
                "amount": values[4],
                "method": values[5],
                "status": values[6],
            }
        )
    return rows


def page_params(code: str, year: int, page: int, page_size: int) -> dict[str, str]:
    return {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "count_record": str(page_size),
        "page": str(page),
        "smb": "",
    }


def fetch_rows_for_code(
    item: TruItem,
    year: int,
    page_size: int,
    max_pages: int | None,
    sleep_seconds: float,
) -> list[list[str]]:
    session = get_session()
    rows: list[list[str]] = []

    first_resp = fetch_lots_page(
        session, code=item.code, year=year, page=1, page_size=page_size
    )
    first_soup = BeautifulSoup(first_resp.text, "lxml")
    total = parse_total_records(first_soup)
    page_count = max(1, math.ceil(total / page_size))
    if max_pages is not None:
        page_count = min(page_count, max_pages)
    rows.extend(_to_output_rows(item, extract_lot_rows(first_soup)))

    for page in range(2, page_count + 1):
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        resp = fetch_lots_page(
            session, code=item.code, year=year, page=page, page_size=page_size
        )
        soup = BeautifulSoup(resp.text, "lxml")
        rows.extend(_to_output_rows(item, extract_lot_rows(soup)))
    return rows


def _to_output_rows(item: TruItem, lot_rows: Iterable[dict[str, str]]) -> list[list[str]]:
    result: list[list[str]] = []
    for lot in lot_rows:
        result.append(
            [
                lot["lot_no"],
                item.code,
                item.name,
                lot["announcement"],
                lot["lot_description"],
                lot["qty"],
                lot["amount"],
                lot["method"],
                lot["status"],
            ]
        )
    return result


def load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    completed = data.get("completed_codes")
    if isinstance(completed, list):
        return {str(x) for x in completed}
    return set()


def save_checkpoint(path: Path, completed_codes: set[str], total_rows: int) -> None:
    payload = {
        "completed_codes": sorted(completed_codes),
        "completed_count": len(completed_codes),
        "rows_written": total_rows,
        "updated_at_unix": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect lots for TRU codes from Google Sheet."
    )
    parser.add_argument("--input-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--target-sheet-id", default=TARGET_SHEET_ID)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--output", default="lots_2025_by_tru.csv")
    parser.add_argument("--checkpoint", default="lots_2025_checkpoint.json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument(
        "--max-pages-per-code",
        type=int,
        default=None,
        help="Limit pages for each code (debug/testing).",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=None,
        help="Limit number of codes to process (debug/testing).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Ignore existing checkpoint and overwrite output file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint)

    print(f"Reading TRU items from sheet {args.input_sheet_id} ...", flush=True)
    items = read_tru_items(args.input_sheet_id)
    if args.max_codes is not None:
        items = items[: args.max_codes]
    if not items:
        print("No TRU codes found.", flush=True)
        return 1
    print(f"Loaded {len(items)} unique TRU codes.", flush=True)

    completed_codes: set[str] = set()
    write_mode = "w"
    if not args.reset:
        completed_codes = load_checkpoint(checkpoint_path)
        if output_path.exists() and output_path.stat().st_size > 0:
            write_mode = "a"
    else:
        if checkpoint_path.exists():
            checkpoint_path.unlink()

    pending = [item for item in items if item.code not in completed_codes]
    print(
        f"Pending codes: {len(pending)} (already completed: {len(completed_codes)})",
        flush=True,
    )
    if not pending:
        print("All codes already processed by checkpoint.", flush=True)
        return 0

    lock = threading.Lock()
    total_rows = 0
    if write_mode == "a":
        # fast estimate for progress display
        try:
            with output_path.open("r", encoding="utf-8", newline="") as in_f:
                total_rows = max(sum(1 for _ in in_f) - 1, 0)
        except Exception:  # noqa: BLE001
            total_rows = 0

    print(
        f"Writing results to {output_path} (mode={write_mode}). "
        f"Target sheet: {args.target_sheet_id}",
        flush=True,
    )

    with output_path.open(write_mode, encoding="utf-8", newline="") as out_f:
        writer = csv.writer(out_f)
        if write_mode == "w":
            writer.writerow(OUTPUT_COLUMNS)
            out_f.flush()

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            future_map = {
                pool.submit(
                    fetch_rows_for_code,
                    item,
                    args.year,
                    args.page_size,
                    args.max_pages_per_code,
                    args.sleep,
                ): item
                for item in pending
            }

            processed = 0
            for future in as_completed(future_map):
                item = future_map[future]
                processed += 1
                try:
                    code_rows = future.result()
                except Exception as err:  # noqa: BLE001
                    print(
                        f"[ERROR] {item.code}: {err}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                with lock:
                    if code_rows:
                        writer.writerows(code_rows)
                        total_rows += len(code_rows)
                        out_f.flush()

                    completed_codes.add(item.code)
                    save_checkpoint(checkpoint_path, completed_codes, total_rows)

                print(
                    f"[{processed}/{len(pending)}] {item.code} -> {len(code_rows)} rows "
                    f"(total written: {total_rows})",
                    flush=True,
                )

    print(
        "\nDone.\n"
        f"Output CSV: {output_path.resolve()}\n"
        f"Checkpoint: {checkpoint_path.resolve()}",
        flush=True,
    )
    print(
        "Note: direct write to Google target sheet requires authenticated Google API access.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
