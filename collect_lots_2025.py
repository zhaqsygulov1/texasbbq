#!/usr/bin/env python3
import argparse
import csv
import io
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

HEADERS = [
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
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": UA})
    return session


def fetch_url(session: requests.Session, url: str, timeout: int = 60) -> str:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, 8):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            sleep_for = min(20, (2 ** (attempt - 1)) + random.random())
            print(f"[WARN] fetch failed ({attempt}/7): {url} -> {exc}; sleep {sleep_for:.1f}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"Cannot fetch URL after retries: {url}; last={last_exc}")


def read_sheet_csv(session: requests.Session, sheet_id: str, gid: int = 0) -> List[List[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = session.get(url, timeout=90)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return list(csv.reader(io.StringIO(resp.text)))


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_table_rows(
    html: str, tru_code: str, tru_name: str
) -> Tuple[List[List[str]], bool]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="search-result")
    if table is None:
        if "Реестр лотов" not in html:
            raise RuntimeError("Unexpected page without results table (possibly blocked page)")
        return [], False

    rows: List[List[str]] = []
    tr_list = table.find_all("tr")
    for tr in tr_list[1:]:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_no = normalize_text(tds[0].get_text(" ", strip=True))

        announce_strong = tds[1].find("strong")
        announce_name = normalize_text(
            announce_strong.get_text(" ", strip=True) if announce_strong else tds[1].get_text(" ", strip=True)
        )

        lot_pieces = [normalize_text(x) for x in tds[2].stripped_strings]
        lot_pieces = [x for x in lot_pieces if x and x.lower() != "история"]
        lot_name_desc = normalize_text(" ".join(lot_pieces))

        qty = normalize_text(tds[3].get_text(" ", strip=True))
        amount = normalize_text(tds[4].get_text(" ", strip=True))
        trade_type = normalize_text(tds[5].get_text(" ", strip=True))
        status = normalize_text(tds[6].get_text(" ", strip=True))

        rows.append(
            [
                lot_no,
                tru_code,
                tru_name,
                announce_name,
                lot_name_desc,
                qty,
                amount,
                trade_type,
                status,
            ]
        )

    next_exists = False
    next_page_num = None
    active = table.find_next("ul", class_="pagination")
    if active is not None:
        active_page_li = active.find("li", class_="active")
        if active_page_li:
            active_text = normalize_text(active_page_li.get_text(" ", strip=True))
            if active_text.isdigit():
                next_page_num = int(active_text) + 1
        if next_page_num is not None:
            for a in active.select("a[href]"):
                href = a.get("href", "")
                m = re.search(r"[?&]page=(\d+)", href)
                if m and int(m.group(1)) == next_page_num:
                    next_exists = True
                    break

    return rows, next_exists


def make_search_url(tru_code: str, page: int) -> str:
    params = [
        ("filter[name]", ""),
        ("filter[number]", ""),
        ("filter[number_anno]", ""),
        ("filter[enstru]", tru_code),
        ("filter[status][]", "360"),
        ("filter[customer]", ""),
        ("filter[amount_from]", "15000000"),
        ("filter[amount_to]", ""),
        ("filter[trade_type]", ""),
        ("filter[month]", ""),
        ("filter[plan_number]", ""),
        ("filter[end_date_from]", ""),
        ("filter[end_date_to]", ""),
        ("filter[start_date_to]", ""),
        ("filter[year]", "2025"),
        ("filter[itogi_date_from]", ""),
        ("filter[itogi_date_to]", ""),
        ("filter[start_date_from]", ""),
        ("filter[more]", ""),
        ("count_record", "50"),
        ("page", str(page)),
        ("smb", ""),
    ]
    return f"{GOSZAKUP_SEARCH_URL}?{urlencode(params)}"


def load_state(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"processed_codes": [], "new_rows": 0}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: Dict[str, object]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def read_existing_keys(rows: Sequence[Sequence[str]]) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for row in rows[1:]:
        if len(row) < 2:
            continue
        lot_no = normalize_text(row[0])
        tru_code = normalize_text(row[1])
        if lot_no and tru_code:
            keys.add((lot_no, tru_code))
    return keys


def read_dest_codes(rows: Sequence[Sequence[str]]) -> Set[str]:
    codes: Set[str] = set()
    for row in rows[1:]:
        if len(row) > 1:
            code = normalize_text(row[1])
            if code:
                codes.add(code)
    return codes


def iter_source_codes(rows: Sequence[Sequence[str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for row in rows[1:]:
        if not row:
            continue
        code = normalize_text(row[0]) if len(row) > 0 else ""
        name = normalize_text(row[1]) if len(row) > 1 else ""
        if code:
            out.append((code, name))
    return out


def ensure_csv_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)


def append_rows(path: Path, rows: Iterable[Sequence[str]]) -> int:
    count = 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def process_code(
    session: requests.Session,
    tru_code: str,
    tru_name: str,
    known_keys: Set[Tuple[str, str]],
    new_keys: Set[Tuple[str, str]],
    max_pages_per_code: int,
    sleep_between_requests: float,
) -> List[List[str]]:
    collected: List[List[str]] = []
    page = 1
    while page <= max_pages_per_code:
        url = make_search_url(tru_code, page)
        html = fetch_url(session, url)
        rows, next_exists = parse_table_rows(html, tru_code, tru_name)

        if not rows:
            break

        for row in rows:
            key = (row[0], row[1])
            if key in known_keys or key in new_keys:
                continue
            new_keys.add(key)
            collected.append(row)

        if not next_exists:
            break

        page += 1
        if sleep_between_requests > 0:
            time.sleep(sleep_between_requests + random.random() * 0.2)

    return collected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect 2025 goszakup lots by TRU codes from source Google Sheet."
    )
    parser.add_argument(
        "--output",
        default="lots_2025_increment.csv",
        help="CSV file for newly found rows",
    )
    parser.add_argument(
        "--state-file",
        default="lots_2025_state.json",
        help="State JSON file for resume support",
    )
    parser.add_argument(
        "--process-all-codes",
        action="store_true",
        help="Process all source codes instead of only codes missing in destination sheet",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=0,
        help="Optional limit of codes to process in current run (0 = no limit)",
    )
    parser.add_argument(
        "--max-pages-per-code",
        type=int,
        default=5000,
        help="Safety guard for pagination per code",
    )
    parser.add_argument(
        "--sleep-between-requests",
        type=float,
        default=0.4,
        help="Delay between page requests for same code",
    )
    args = parser.parse_args()

    session = build_session()
    print("[INFO] Loading source sheet...")
    source_rows = read_sheet_csv(session, SOURCE_SHEET_ID, gid=0)
    source_codes = iter_source_codes(source_rows)
    source_map = {code: name for code, name in source_codes}
    print(f"[INFO] Source codes: {len(source_codes)}")

    print("[INFO] Loading destination sheet...")
    dest_rows = read_sheet_csv(session, TARGET_SHEET_ID, gid=0)
    known_keys = read_existing_keys(dest_rows)
    dest_codes = read_dest_codes(dest_rows)
    print(f"[INFO] Existing destination rows: {len(dest_rows) - 1}")
    print(f"[INFO] Existing destination code coverage: {len(dest_codes)}")

    if args.process_all_codes:
        target_codes = [code for code, _ in source_codes]
    else:
        target_codes = [code for code, _ in source_codes if code not in dest_codes]

    state_path = Path(args.state_file)
    state = load_state(state_path)
    processed_codes = set(state.get("processed_codes", []))
    already_new_rows = int(state.get("new_rows", 0))

    pending_codes = [c for c in target_codes if c not in processed_codes]
    if args.max_codes and args.max_codes > 0:
        pending_codes = pending_codes[: args.max_codes]

    print(f"[INFO] Target codes total: {len(target_codes)}")
    print(f"[INFO] Already processed by state: {len(processed_codes)}")
    print(f"[INFO] Codes to process now: {len(pending_codes)}")

    output_path = Path(args.output)
    ensure_csv_header(output_path)

    new_keys: Set[Tuple[str, str]] = set()
    total_new_rows = 0
    run_started = time.time()

    for idx, code in enumerate(pending_codes, start=1):
        tru_name = source_map.get(code, "")
        try:
            rows = process_code(
                session=session,
                tru_code=code,
                tru_name=tru_name,
                known_keys=known_keys,
                new_keys=new_keys,
                max_pages_per_code=args.max_pages_per_code,
                sleep_between_requests=args.sleep_between_requests,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] code={code} failed: {exc}")
            # Keep code unprocessed for next run.
            continue

        if rows:
            written = append_rows(output_path, rows)
            total_new_rows += written

        processed_codes.add(code)
        state["processed_codes"] = sorted(processed_codes)
        state["new_rows"] = already_new_rows + total_new_rows
        save_state(state_path, state)

        elapsed = time.time() - run_started
        rate = idx / elapsed if elapsed > 0 else 0
        eta = (len(pending_codes) - idx) / rate if rate > 0 else 0
        print(
            f"[INFO] {idx}/{len(pending_codes)} code={code} "
            f"new_rows={len(rows)} run_total={total_new_rows} "
            f"eta={eta/60:.1f}m"
        )

    print("[DONE]")
    print(f"[DONE] New rows in this run: {total_new_rows}")
    print(f"[DONE] Cumulative new rows by state: {already_new_rows + total_new_rows}")
    print(f"[DONE] Output: {output_path.resolve()}")
    print(f"[DONE] State: {state_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
