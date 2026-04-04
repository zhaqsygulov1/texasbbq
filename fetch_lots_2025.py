#!/usr/bin/env python3
import argparse
import csv
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from urllib.parse import parse_qs, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_GID = "0"
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
REQUEST_TIMEOUT = 45
COUNT_RECORD = 2000

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


def fetch_source_codes(sheet_id: str, gid: str) -> list[dict[str, str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    records: list[dict[str, str]] = []
    reader = csv.DictReader(StringIO(resp.text))
    for row in reader:
        code = (row.get("Код ТРУ") or "").strip()
        name = (row.get("Название") or "").strip()
        if code:
            records.append({"code": code, "name": name})
    return records


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a_tag in soup.select("ul.pagination a[href]"):
        href = a_tag.get("href", "")
        parsed = urlparse(href)
        page_values = parse_qs(parsed.query).get("page")
        if page_values:
            try:
                max_page = max(max_page, int(page_values[0]))
            except ValueError:
                pass
    return max_page


def parse_rows(soup: BeautifulSoup, code: str, item_name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    table = soup.find("table", id="search-result")
    if not table:
        return rows

    tbody = table.find("tbody")
    if not tbody:
        return rows

    for tr in tbody.find_all("tr"):
        # The source table markup is not strict HTML (missing closing </td>),
        # so we parse all <td> cells, not only direct children.
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number_tag = tds[0].find("strong")
        lot_number = (
            lot_number_tag.get_text(" ", strip=True)
            if lot_number_tag
            else tds[0].get_text(" ", strip=True)
        )

        announce_name_tag = tds[1].find("strong")
        announce_name = (
            announce_name_tag.get_text(" ", strip=True)
            if announce_name_tag
            else tds[1].get_text(" ", strip=True)
        )

        lot_desc_tag = tds[2].find("strong")
        lot_desc = (
            lot_desc_tag.get_text(" ", strip=True)
            if lot_desc_tag
            else tds[2].get_text(" ", strip=True)
        )
        qty = tds[3].get_text(" ", strip=True)
        amount = tds[4].get_text(" ", strip=True)
        method = tds[5].get_text(" ", strip=True)
        status = tds[6].get_text(" ", strip=True)

        rows.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": code,
                "Наименование товара": item_name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_desc,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": method,
                "Статус": status,
            }
        )
    return rows


def build_params(code: str, year: int, page: int) -> dict[str, str]:
    return {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[customer]": "",
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "filter[year]": str(year),
        "filter[status][]": "360",
        "filter[amount_from]": "15000000",
        "count_record": str(COUNT_RECORD),
        "page": str(page),
        "smb": "",
    }


def fetch_page_html(session: requests.Session, code: str, year: int, page: int) -> str:
    params = build_params(code=code, year=year, page=page)
    last_err: Exception | None = None
    for attempt in range(1, 5):
        try:
            resp = session.get(
                BASE_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
                verify=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; lot-scraper/1.0)"},
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt == 4:
                break
    raise RuntimeError(f"Failed to fetch code={code} page={page}: {last_err}") from last_err


def fetch_code_rows(code: str, name: str, year: int) -> list[dict[str, str]]:
    with requests.Session() as session:
        page_one_html = fetch_page_html(session=session, code=code, year=year, page=1)
        soup = BeautifulSoup(page_one_html, "html.parser")
        rows = parse_rows(soup, code=code, item_name=name)
        max_page = parse_max_page(soup)

        if max_page <= 1:
            return rows

        for page in range(2, max_page + 1):
            html = fetch_page_html(session=session, code=code, year=year, page=page)
            page_soup = BeautifulSoup(html, "html.parser")
            rows.extend(parse_rows(page_soup, code=code, item_name=name))

        return rows


def write_results(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for row in rows:
        key = (row["№ лота"], row["Код ТРУ"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Собирает лоты goszakup за 2025 год по кодам ТРУ из Google Sheet "
            "(по фильтру: Закупка состоялась, сумма >= 15 000 000)."
        )
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--offset-codes", type=int, default=0)
    parser.add_argument("--output", default="lots_2025_filtered.csv")
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID)
    parser.add_argument("--source-gid", default=SOURCE_GID)
    args = parser.parse_args()

    codes = fetch_source_codes(sheet_id=args.source_sheet_id, gid=args.source_gid)
    if args.offset_codes > 0:
        codes = codes[args.offset_codes :]
    if args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    total_codes = len(codes)
    print(f"Loaded {total_codes} ТРУ codes")

    all_rows: list[dict[str, str]] = []
    errors: list[str] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_code = {
            executor.submit(fetch_code_rows, rec["code"], rec["name"], args.year): rec
            for rec in codes
        }

        for i, future in enumerate(as_completed(future_to_code), start=1):
            rec = future_to_code[future]
            code = rec["code"]
            try:
                rows = future.result()
                with lock:
                    all_rows.extend(rows)
                print(f"[{i}/{total_codes}] {code}: {len(rows)} rows")
            except Exception as exc:  # noqa: BLE001
                msg = f"{code}: {exc}"
                errors.append(msg)
                print(f"[{i}/{total_codes}] ERROR {msg}")

    deduped = dedupe_rows(all_rows)
    # Stable sort for reproducible output.
    deduped.sort(key=lambda r: (r["Код ТРУ"], r["№ лота"]))
    write_results(args.output, deduped)

    print(f"Total rows (raw): {len(all_rows)}")
    print(f"Total rows (deduped): {len(deduped)}")
    print(f"Saved to: {args.output}")
    if errors:
        print(f"Errors: {len(errors)}")
        with open("lots_2025_errors.log", "w", encoding="utf-8") as f:
            f.write("\n".join(errors))
        print("Error log saved to lots_2025_errors.log")


if __name__ == "__main__":
    main()
