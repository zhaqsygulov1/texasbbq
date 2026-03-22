#!/usr/bin/env python3
"""Collect 2025 goszakup lots by TRU codes from a Google Sheet.

The script reads TRU codes from a source Google Sheet, queries goszakup lots,
and writes a CSV with the target columns:
№ лота, Код ТРУ, Наименование товара, Наименование объявления,
Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус.

By default it uses the same filters as in the provided example URL:
- year = 2025
- status = 360 (Закупка состоялась)
- amount_from = 15000000
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEST_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GID = 0

CSV_COLUMNS = [
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

LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"
TOTAL_RE = re.compile(
    r"Показано c\s*(\d+)\s*по\s*(\d+)\s*из\s*([\d\s]+)\s*записей", re.IGNORECASE
)


@dataclass(frozen=True)
class TruItem:
    code: str
    name: str


def export_csv_url(sheet_id: str, gid: int = GID) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def normalize_spaces(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def decode_csv_content(content: bytes) -> str:
    # Google Sheets export is UTF-8 with optional BOM.
    return content.decode("utf-8-sig", errors="replace")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en;q=0.9",
        }
    )
    return s


def fetch_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    retries: int = 4,
    sleep_seconds: float = 1.5,
) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - network retries
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(sleep_seconds * attempt)
    assert last_exc is not None
    raise last_exc


def read_source_codes(
    session: requests.Session, source_sheet_id: str
) -> List[TruItem]:
    response = fetch_with_retry(session, export_csv_url(source_sheet_id))
    text = decode_csv_content(response.content)
    reader = csv.DictReader(io.StringIO(text))

    seen = set()
    rows: List[TruItem] = []
    for row in reader:
        code = normalize_spaces(row.get("Код ТРУ", "").strip())
        if not code or code in seen:
            continue
        name = normalize_spaces(row.get("Название", "").strip())
        rows.append(TruItem(code=code, name=name))
        seen.add(code)
    return rows


def parse_total_records(html: str) -> int:
    m = TOTAL_RE.search(html)
    if not m:
        return 0
    raw = m.group(3)
    return int(raw.replace(" ", ""))


def parse_lot_rows(
    html: str, tru_code: str, tru_name: str
) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "search-result"})
    if table is None:
        return []

    tbody = table.find("tbody")
    if tbody is None:
        return []

    parsed: List[Dict[str, str]] = []
    for tr in tbody.find_all("tr", recursive=False):
        # The markup has unclosed <td> tags in some rows, so recursive=False
        # may return only one cell. Parsing all descendant td tags is safer.
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        def first_strong_text(cell) -> str:
            strong = cell.find("strong")
            if strong:
                return normalize_spaces(strong.get_text(" ", strip=True))
            return normalize_spaces(cell.get_text(" ", strip=True))

        lot_no = first_strong_text(tds[0])
        announce_name = first_strong_text(tds[1])
        lot_name = first_strong_text(tds[2])
        qty = normalize_spaces(tds[3].get_text(" ", strip=True))
        amount = first_strong_text(tds[4])
        trade_method = normalize_spaces(tds[5].get_text(" ", strip=True))
        status = normalize_spaces(tds[6].get_text(" ", strip=True))

        if not lot_no:
            continue

        parsed.append(
            {
                "№ лота": lot_no,
                "Код ТРУ": tru_code,
                "Наименование товара": tru_name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_name,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": trade_method,
                "Статус": status,
            }
        )
    return parsed


def build_query_params(
    tru_code: str,
    *,
    year: int,
    status: Optional[str],
    amount_from: Optional[int],
) -> Dict[str, str]:
    params: Dict[str, str] = {
        "filter[enstru]": tru_code,
        "filter[year]": str(year),
        "smb": "",
    }
    if status:
        params["filter[status][]"] = status
    if amount_from is not None:
        params["filter[amount_from]"] = str(amount_from)
    return params


def collect_for_code(
    session: requests.Session,
    item: TruItem,
    *,
    year: int,
    status: Optional[str],
    amount_from: Optional[int],
    max_rows_per_code: int,
) -> Tuple[List[Dict[str, str]], int]:
    params = build_query_params(
        item.code, year=year, status=status, amount_from=amount_from
    )

    first_response = fetch_with_retry(session, LOTS_URL, params=params)
    first_html = first_response.text
    total = parse_total_records(first_html)
    if total == 0:
        return [], 0

    capped_total = min(total, max_rows_per_code)
    pages = max(1, math.ceil(capped_total / 50))

    all_rows = parse_lot_rows(first_html, item.code, item.name)
    if pages > 1:
        for page in range(2, pages + 1):
            params_page = dict(params)
            params_page["page"] = str(page)
            html = fetch_with_retry(session, LOTS_URL, params=params_page).text
            all_rows.extend(parse_lot_rows(html, item.code, item.name))
            # Small pause to avoid being throttled by the target website.
            time.sleep(0.1)

    if len(all_rows) > max_rows_per_code:
        all_rows = all_rows[:max_rows_per_code]

    return all_rows, total


def dedupe_rows(rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    unique_rows = []
    for row in rows:
        key = (row["№ лота"], row["Код ТРУ"])
        if key in seen:
            continue
        unique_rows.append(row)
        seen.add(key)
    return unique_rows


def collect_for_code_worker(
    item: TruItem,
    *,
    year: int,
    status: Optional[str],
    amount_from: Optional[int],
    max_rows_per_code: int,
) -> Tuple[TruItem, List[Dict[str, str]], int]:
    session = make_session()
    rows, total = collect_for_code(
        session,
        item,
        year=year,
        status=status,
        amount_from=amount_from,
        max_rows_per_code=max_rows_per_code,
    )
    return item, rows, total


def write_csv(path: Path, rows: Sequence[Dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def try_write_google_sheet(
    output_rows: Sequence[Dict[str, str]],
    *,
    sheet_id: str,
    gid: int,
) -> bool:
    """Try to write rows to a Google Sheet using service account credentials.

    Expected credential sources (first found wins):
    - GOOGLE_SERVICE_ACCOUNT_JSON: raw JSON text
    - GOOGLE_SERVICE_ACCOUNT_FILE: path to service account JSON file

    Returns True if write succeeded, False otherwise.
    """
    import json
    import os
    import tempfile

    cred_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    cred_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    if not cred_json and not cred_file:
        return False

    try:
        import gspread  # type: ignore
        from gspread.utils import rowcol_to_a1  # type: ignore
    except Exception:
        return False

    tmp_path = None
    try:
        if cred_json:
            fd, path = tempfile.mkstemp(prefix="gsa_", suffix=".json")
            tmp_path = path
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(json.loads(cred_json), f)
            auth_path = path
        else:
            auth_path = cred_file

        gc = gspread.service_account(filename=auth_path)
        sh = gc.open_by_key(sheet_id)
        ws = sh.get_worksheet_by_id(gid)
        if ws is None:
            ws = sh.get_worksheet(0)
            if ws is None:
                return False

        values = [CSV_COLUMNS]
        values.extend([[row.get(col, "") for col in CSV_COLUMNS] for row in output_rows])

        end_cell = rowcol_to_a1(max(1, len(values)), len(CSV_COLUMNS))
        ws.batch_clear([f"A:{rowcol_to_a1(1, len(CSV_COLUMNS)).rstrip('1')}"])
        ws.update(f"A1:{end_cell}", values, value_input_option="RAW")
        return True
    except Exception:
        return False
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect goszakup lots for TRU codes from Google Sheet."
    )
    parser.add_argument(
        "--source-sheet-id", default=SOURCE_SHEET_ID, help="Google Sheet ID with TRU codes"
    )
    parser.add_argument(
        "--dest-sheet-id",
        default=DEST_SHEET_ID,
        help="Google Sheet ID to try writing results into (optional)",
    )
    parser.add_argument("--gid", type=int, default=GID, help="Worksheet gid")
    parser.add_argument("--year", type=int, default=2025, help="Financial year filter")
    parser.add_argument(
        "--status",
        default="360",
        help="Status filter code (default: 360 = Закупка состоялась); empty to disable",
    )
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15000000,
        help="Minimum lot amount filter; set -1 to disable",
    )
    parser.add_argument(
        "--max-rows-per-code",
        type=int,
        default=10000,
        help="Safety cap of rows per TRU code",
    )
    parser.add_argument(
        "--output-csv",
        default="lots_2025_by_tru.csv",
        help="Local CSV output path",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Optional limit of TRU codes to process (0 = all)",
    )
    parser.add_argument(
        "--only-codes",
        default="",
        help="Comma-separated TRU codes to process exclusively",
    )
    parser.add_argument(
        "--write-google-sheet",
        action="store_true",
        help="Attempt writing results into destination Google Sheet via service account",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel worker count for TRU code processing",
    )
    args = parser.parse_args(argv)

    status = args.status.strip() if args.status and args.status.strip() else None
    amount_from = args.amount_from if args.amount_from >= 0 else None

    session = make_session()
    items = read_source_codes(session, args.source_sheet_id)
    if args.only_codes.strip():
        requested = {
            normalize_spaces(code).strip()
            for code in args.only_codes.split(",")
            if normalize_spaces(code).strip()
        }
        items = [item for item in items if item.code in requested]
    if args.limit_codes > 0:
        items = items[: args.limit_codes]
    print(f"Loaded {len(items)} unique TRU codes from source sheet.")

    all_rows: List[Dict[str, str]] = []
    totals_over_cap = 0
    completed = 0
    workers = max(1, args.workers)
    if workers == 1:
        for idx, item in enumerate(items, start=1):
            try:
                rows, total = collect_for_code(
                    session,
                    item,
                    year=args.year,
                    status=status,
                    amount_from=amount_from,
                    max_rows_per_code=args.max_rows_per_code,
                )
            except Exception as exc:  # noqa: BLE001 - continue on per-code errors
                print(f"[{idx}/{len(items)}] ERROR {item.code}: {exc}", file=sys.stderr)
                continue

            if total > args.max_rows_per_code:
                totals_over_cap += 1
            all_rows.extend(rows)
            print(
                f"[{idx}/{len(items)}] {item.code}: total={total}, collected={len(rows)}, "
                f"accumulated={len(all_rows)}"
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    collect_for_code_worker,
                    item,
                    year=args.year,
                    status=status,
                    amount_from=amount_from,
                    max_rows_per_code=args.max_rows_per_code,
                ): item
                for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                completed += 1
                try:
                    item_ret, rows, total = future.result()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[{completed}/{len(items)}] ERROR {item.code}: {exc}",
                        file=sys.stderr,
                    )
                    continue

                if total > args.max_rows_per_code:
                    totals_over_cap += 1
                all_rows.extend(rows)
                print(
                    f"[{completed}/{len(items)}] {item_ret.code}: total={total}, "
                    f"collected={len(rows)}, accumulated={len(all_rows)}"
                )

    unique_rows = dedupe_rows(all_rows)
    output_path = Path(args.output_csv)
    write_csv(output_path, unique_rows)
    print(f"Saved {len(unique_rows)} unique rows to {output_path.resolve()}")
    if totals_over_cap:
        print(
            f"Warning: {totals_over_cap} code(s) exceeded per-code cap "
            f"({args.max_rows_per_code})."
        )

    if args.write_google_sheet:
        ok = try_write_google_sheet(unique_rows, sheet_id=args.dest_sheet_id, gid=args.gid)
        if ok:
            print(f"Google Sheet updated: https://docs.google.com/spreadsheets/d/{args.dest_sheet_id}")
        else:
            print(
                "Google Sheet write skipped/failed (missing credentials or no access).",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
