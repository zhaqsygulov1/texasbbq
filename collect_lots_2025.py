#!/usr/bin/env python3
"""
Collect 2025 lots from goszakup by TRU codes from Google Sheets.

Output columns:
№ лота, Код ТРУ, Наименование товара, Наименование объявления,
Наименование и описание лота, Кол-во, Сумма, тг., Способ закупки, Статус
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


DEFAULT_SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEFAULT_DEST_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
CSV_HEADERS = [
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


thread_local = threading.local()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )
    return session


def get_session() -> requests.Session:
    if not hasattr(thread_local, "session"):
        thread_local.session = build_session()
    return thread_local.session


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("История", "").strip()
    return text


def extract_text(element) -> str:
    if element is None:
        return ""
    return normalize_text(element.get_text(" ", strip=True))


def export_sheet_as_csv(sheet_id: str, gid: str = "0") -> str:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace")


def read_source_codes(sheet_id: str, gid: str = "0") -> list[TruCode]:
    csv_text = export_sheet_as_csv(sheet_id=sheet_id, gid=gid)
    rows = csv.reader(io.StringIO(csv_text))
    header = next(rows, None)
    if not header:
        raise RuntimeError("Source sheet is empty")

    codes: list[TruCode] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        code = normalize_text(row[0]) if len(row) >= 1 else ""
        name = normalize_text(row[1]) if len(row) >= 2 else ""
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(TruCode(code=code, name=name))
    return codes


def fetch_html(params: dict[str, str], attempts: int = 6) -> str:
    session = get_session()
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=90)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == attempts:
                break
            sleep_seconds = (2 ** (attempt - 1)) + random.uniform(0, 0.75)
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Failed to fetch {params}: {last_exc}") from last_exc


def parse_total_pages(soup: BeautifulSoup) -> int:
    pages = [1]
    for link in soup.select("ul.pagination a[href]"):
        href = link.get("href", "")
        match = re.search(r"[?&]page=(\d+)", href)
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def parse_rows_for_code(soup: BeautifulSoup, code: str, product_name: str) -> list[list[str]]:
    tbody = soup.select_one("table#search-result tbody")
    if not tbody:
        return []

    rows: list[list[str]] = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue

        lot_number = extract_text(tds[0])
        announcement = extract_text(tds[1].find("strong")) or extract_text(tds[1])

        lot_name = extract_text(tds[2].find("strong"))
        lot_details_candidates = []
        for small in tds[2].find_all("small", class_="hidden-xs"):
            val = extract_text(small)
            if val:
                lot_details_candidates.append(val)
        lot_details = " | ".join(lot_details_candidates)
        if lot_name and lot_details:
            lot_name_and_desc = f"{lot_name} | {lot_details}"
        elif lot_name:
            lot_name_and_desc = lot_name
        else:
            lot_name_and_desc = extract_text(tds[2])

        quantity = extract_text(tds[3])
        amount = extract_text(tds[4])
        method = extract_text(tds[5])
        status = extract_text(tds[6])

        rows.append(
            [
                lot_number,
                code,
                product_name,
                announcement,
                lot_name_and_desc,
                quantity,
                amount,
                method,
                status,
            ]
        )
    return rows


def collect_for_code(
    tru: TruCode,
    year: int,
    count_record: int,
    status_filter: list[str] | None = None,
    amount_from: str | None = None,
    amount_to: str | None = None,
) -> tuple[str, list[list[str]]]:
    base_params: dict[str, str] = {
        "filter[enstru]": tru.code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "smb": "",
    }
    if amount_from:
        base_params["filter[amount_from]"] = amount_from
    if amount_to:
        base_params["filter[amount_to]"] = amount_to

    if status_filter:
        # requests can't have duplicate keys in dict, so we encode manually
        # by sending one of the statuses here and expanding query as tuples below.
        pass

    all_rows: list[list[str]] = []
    seen_keys: set[tuple[str, str]] = set()

    def merge_rows(candidate_rows: Iterable[list[str]]) -> None:
        for row in candidate_rows:
            key = (row[0], row[1])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_rows.append(row)

    params_page1 = dict(base_params)
    params_page1["page"] = "1"

    html = fetch_html(params=params_page1)
    soup = BeautifulSoup(html, "html.parser")
    merge_rows(parse_rows_for_code(soup=soup, code=tru.code, product_name=tru.name))
    total_pages = parse_total_pages(soup)

    if total_pages > 1:
        for page in range(2, total_pages + 1):
            params = dict(base_params)
            params["page"] = str(page)
            page_html = fetch_html(params=params)
            page_soup = BeautifulSoup(page_html, "html.parser")
            merge_rows(
                parse_rows_for_code(page_soup, code=tru.code, product_name=tru.name)
            )
            # small jitter keeps request pattern less bursty
            time.sleep(random.uniform(0.03, 0.12))

    return tru.code, all_rows


def write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)


def upload_to_google_sheet(
    spreadsheet_id: str,
    rows: list[list[str]],
    oauth_token: str,
    worksheet_range: str = "A1:I",
    chunk_size: int = 5000,
) -> None:
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "Content-Type": "application/json",
    }

    clear_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{quote(worksheet_range, safe='')}:clear"
    )
    clear_resp = requests.post(clear_url, headers=headers, json={}, timeout=90)
    clear_resp.raise_for_status()

    all_values = [CSV_HEADERS] + rows
    for start in range(0, len(all_values), chunk_size):
        chunk = all_values[start : start + chunk_size]
        start_row = start + 1
        end_row = start + len(chunk)
        target_range = f"A{start_row}:I{end_row}"
        update_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
            f"{quote(target_range, safe='')}?valueInputOption=RAW"
        )
        payload = {"majorDimension": "ROWS", "values": chunk}
        resp = requests.put(update_url, headers=headers, json=payload, timeout=90)
        resp.raise_for_status()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect 2025 lots by TRU codes and build output CSV."
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--source-sheet-id", default=DEFAULT_SOURCE_SHEET_ID)
    parser.add_argument("--source-gid", default="0")
    parser.add_argument("--dest-sheet-id", default=DEFAULT_DEST_SHEET_ID)
    parser.add_argument(
        "--output",
        default="output/lots_2025_by_tru.csv",
        help="CSV output path",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload result to destination sheet using OAuth token",
    )
    parser.add_argument(
        "--oauth-token-env",
        default="GOOGLE_OAUTH_TOKEN",
        help="Env var with OAuth token for --upload mode",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codes = read_source_codes(sheet_id=args.source_sheet_id, gid=args.source_gid)
    if args.limit_codes and args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    print(f"Loaded TRU codes: {len(codes)}")
    print(
        f"Collecting lots for year {args.year} with workers={args.workers}, "
        f"count_record={args.count_record}"
    )

    results_by_code: dict[str, list[list[str]]] = {}
    total = len(codes)
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                collect_for_code,
                tru,
                args.year,
                args.count_record,
            ): tru
            for tru in codes
        }

        for future in as_completed(futures):
            tru = futures[future]
            done += 1
            try:
                code, rows = future.result()
                results_by_code[code] = rows
                print(f"[{done}/{total}] {code}: {len(rows)} rows")
            except Exception as exc:
                print(f"[{done}/{total}] {tru.code}: ERROR: {exc}")
                results_by_code[tru.code] = []

    ordered_rows: list[list[str]] = []
    for tru in codes:
        ordered_rows.extend(results_by_code.get(tru.code, []))

    output_path = Path(args.output)
    write_csv(output_path, ordered_rows)
    print(f"Wrote {len(ordered_rows)} rows to {output_path}")

    if args.upload:
        token = os.getenv(args.oauth_token_env, "").strip()
        if not token:
            raise RuntimeError(
                f"--upload specified, but {args.oauth_token_env} is not set"
            )
        upload_to_google_sheet(
            spreadsheet_id=args.dest_sheet_id,
            rows=ordered_rows,
            oauth_token=token,
        )
        print(f"Uploaded rows to Google Sheet: {args.dest_sheet_id}")
    else:
        print("Upload skipped. Use --upload with OAuth token to update Google Sheet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
