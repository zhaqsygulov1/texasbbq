#!/usr/bin/env python3
import argparse
import csv
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import StringIO
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_CODES_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
OUTPUT_CSV = "lots_2025_by_tru_codes.csv"

OUT_HEADERS = [
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

thread_local = threading.local()


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


def get_session() -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            }
        )
        thread_local.session = session
    return session


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_tru_codes(url: str) -> List[TruCode]:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    decoded = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(decoded))
    if not reader.fieldnames:
        return []
    code_key = next((name for name in reader.fieldnames if "Код" in name), reader.fieldnames[0])
    name_key = next((name for name in reader.fieldnames if "Назв" in name), reader.fieldnames[-1])
    result: List[TruCode] = []
    seen = set()
    for row in reader:
        code = normalize_space((row.get(code_key) or ""))
        name = normalize_space((row.get(name_key) or ""))
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(TruCode(code=code, name=name))
    return result


def base_params(code: str, year: int, status: str, amount_from: str, page: int) -> Dict[str, str]:
    return {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "filter[status][]": status,
        "filter[amount_from]": amount_from,
        "smb": "",
        "count_record": "50",
        "page": str(page),
    }


def fetch_page(code: str, year: int, status: str, amount_from: str, page: int) -> str:
    params = base_params(code, year, status, amount_from, page)
    last_error: Optional[Exception] = None
    for attempt in range(1, 5):
        try:
            response = get_session().get(SEARCH_URL, params=params, timeout=45)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # pylint: disable=broad-except
            last_error = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Failed to fetch code={code} page={page}: {last_error}") from last_error


def extract_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [normalize_space(th.get_text(" ", strip=True)) for th in table.select("th")]
        if "№ лота" in headers and "Статус" in headers:
            return table
    return None


def parse_total_records(soup: BeautifulSoup) -> Optional[int]:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей", text)
    if not match:
        return None
    return int(match.group(1).replace(" ", ""))


def first_link_text(td) -> str:
    link = td.find("a")
    if link:
        return normalize_space(link.get_text(" ", strip=True))
    return normalize_space(td.get_text(" ", strip=True))


def parse_rows(html: str, code: str, code_name: str) -> Tuple[List[Dict[str, str]], Optional[int]]:
    soup = BeautifulSoup(html, "lxml")
    total = parse_total_records(soup)
    table = extract_table(soup)
    if table is None:
        return [], total
    rows: List[Dict[str, str]] = []
    for tr in table.select("tr"):
        tds = tr.select("td")
        if len(tds) < 7:
            continue
        lot_info_text = normalize_space(tds[2].get_text(" ", strip=True)).replace(" История", "")
        rows.append(
            {
                "№ лота": normalize_space(tds[0].get_text(" ", strip=True)),
                "Код ТРУ": code,
                "Наименование товара": code_name,
                "Наименование объявления": first_link_text(tds[1]),
                "Наименование и описание лота": lot_info_text,
                "Кол-во": normalize_space(tds[3].get_text(" ", strip=True)),
                "Сумма, тг.": normalize_space(tds[4].get_text(" ", strip=True)),
                "Способ закупки": normalize_space(tds[5].get_text(" ", strip=True)),
                "Статус": normalize_space(tds[6].get_text(" ", strip=True)),
            }
        )
    return rows, total


def collect_for_code(tru: TruCode, year: int, status: str, amount_from: str) -> Tuple[str, List[Dict[str, str]], int]:
    html = fetch_page(tru.code, year, status, amount_from, page=1)
    first_rows, total = parse_rows(html, tru.code, tru.name)
    if total is None:
        total = len(first_rows)
    pages = max(1, math.ceil(total / 50))

    all_rows = list(first_rows)
    if pages > 1:
        for page in range(2, pages + 1):
            page_html = fetch_page(tru.code, year, status, amount_from, page=page)
            page_rows, _ = parse_rows(page_html, tru.code, tru.name)
            if not page_rows:
                break
            all_rows.extend(page_rows)
    return tru.code, all_rows, total


def write_csv(rows: Iterable[Dict[str, str]], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def deduplicate_rows(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    deduped: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        key = (row["№ лота"], row["Код ТРУ"], row["Наименование объявления"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect goszakup 2025 lots by TRU codes.")
    parser.add_argument("--source-url", default=SOURCE_CODES_URL)
    parser.add_argument("--output", default=OUTPUT_CSV)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", default="360")
    parser.add_argument("--amount-from", default="15000000")
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args()

    tru_codes = load_tru_codes(args.source_url)
    print(f"Loaded {len(tru_codes)} unique TRU codes")

    combined_rows: List[Dict[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                collect_for_code,
                tru=tru,
                year=args.year,
                status=args.status,
                amount_from=args.amount_from,
            ): tru
            for tru in tru_codes
        }
        for future in as_completed(futures):
            tru = futures[future]
            done += 1
            try:
                _, rows, total = future.result()
                combined_rows.extend(rows)
                print(
                    f"[{done}/{len(tru_codes)}] {tru.code}: "
                    f"total={total}, collected={len(rows)}"
                )
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[{done}/{len(tru_codes)}] {tru.code}: ERROR {exc}")

    deduped_rows = deduplicate_rows(combined_rows)
    write_csv(deduped_rows, args.output)
    print(
        f"Finished. Raw rows={len(combined_rows)}, "
        f"deduped={len(deduped_rows)} -> {args.output}"
    )


if __name__ == "__main__":
    main()
