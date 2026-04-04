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
from typing import Dict, Iterable, List, Sequence, Set, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_SHEET_ID_DEFAULT = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_GID_DEFAULT = "0"
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
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


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def decode_response_content(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        read=5,
        connect=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def load_source_codes(
    session: requests.Session, sheet_id: str, gid: str
) -> List[Tuple[str, str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    response = session.get(url, timeout=90)
    response.raise_for_status()
    text = decode_response_content(response.content)

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []

    data = rows[1:] if rows and len(rows[0]) >= 1 else rows
    result: List[Tuple[str, str]] = []
    seen_codes: Set[str] = set()

    for row in data:
        if not row:
            continue
        code = normalize_text(row[0]) if len(row) >= 1 else ""
        name = normalize_text(row[1]) if len(row) >= 2 else ""
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        result.append((code, name))

    return result


def load_checkpoint(checkpoint_path: Path) -> Set[str]:
    if not checkpoint_path.exists():
        return set()
    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    processed = data.get("processed_codes", [])
    if not isinstance(processed, list):
        return set()
    return {normalize_text(x) for x in processed if isinstance(x, str) and x.strip()}


def save_checkpoint(checkpoint_path: Path, processed_codes: Sequence[str]) -> None:
    checkpoint_path.write_text(
        json.dumps({"processed_codes": list(processed_codes)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_existing_keys(output_csv: Path) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    if not output_csv.exists():
        return keys
    with output_csv.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            lot_no = normalize_text(row.get("№ лота", ""))
            code = normalize_text(row.get("Код ТРУ", ""))
            if lot_no and code:
                keys.add((lot_no, code))
    return keys


def parse_max_page(soup: BeautifulSoup) -> int:
    pages = [1]
    for anchor in soup.select("ul.pagination a[href]"):
        href = anchor.get("href", "")
        match = re.search(r"(?:\?|&)page=(\d+)", href)
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def find_lot_table(soup: BeautifulSoup):
    for table in soup.select("table.table"):
        headers = [normalize_text(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        if "№ лота" in headers and "Способ закупки" in headers and "Статус" in headers:
            return table
    return None


def parse_lot_rows(
    soup: BeautifulSoup, code: str, product_name: str
) -> List[Dict[str, str]]:
    table = find_lot_table(soup)
    if table is None:
        return []

    rows: List[Dict[str, str]] = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 7:
            continue

        lot_number_node = cells[0].find("strong")
        lot_number = normalize_text(
            lot_number_node.get_text(" ", strip=True)
            if lot_number_node
            else cells[0].get_text(" ", strip=True)
        )
        announcement = normalize_text(cells[1].get_text(" ", strip=True))
        lot_desc = normalize_text(cells[2].get_text(" ", strip=True))
        qty = normalize_text(cells[3].get_text(" ", strip=True))
        amount = normalize_text(cells[4].get_text(" ", strip=True))
        procurement_method = normalize_text(cells[5].get_text(" ", strip=True))
        status = normalize_text(cells[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        rows.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": code,
                "Наименование товара": product_name,
                "Наименование объявления": announcement,
                "Наименование и описание лота": lot_desc,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": procurement_method,
                "Статус": status,
            }
        )
    return rows


def fetch_page_html(
    session: requests.Session,
    code: str,
    page: int,
    year: int,
    status: int,
    amount_from: int,
    timeout: int,
) -> str:
    params = {
        "filter[enstru]": code,
        "filter[status][0]": str(status),
        "filter[amount_from]": str(amount_from),
        "filter[year]": str(year),
        "count_record": "50",
        "page": str(page),
        "smb": "",
    }

    last_exc = None
    for attempt in range(1, 6):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_exc = exc
            sleep_seconds = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
            time.sleep(sleep_seconds)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unexpected request failure without captured exception.")


def ensure_output_writer(output_csv: Path):
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    file_exists = output_csv.exists()
    fh = output_csv.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=OUTPUT_HEADERS)
    if not file_exists:
        writer.writeheader()
        fh.flush()
    return fh, writer


def iter_codes(codes: Sequence[Tuple[str, str]], max_codes: int | None) -> Iterable[Tuple[str, str]]:
    if max_codes is None:
        yield from codes
    else:
        yield from codes[:max_codes]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сбор лотов 2025 года по списку кодов ТРУ из Google Sheets."
    )
    parser.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID_DEFAULT)
    parser.add_argument("--source-gid", default=SOURCE_GID_DEFAULT)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", type=int, default=360)
    parser.add_argument("--amount-from", type=int, default=15000000)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output-csv", default="out/lots_2025_by_tru.csv")
    parser.add_argument("--checkpoint", default="out/lots_2025_checkpoint.json")
    parser.add_argument("--max-codes", type=int, default=None)
    args = parser.parse_args()

    session = make_session()
    output_csv = Path(args.output_csv)
    checkpoint_path = Path(args.checkpoint)

    source_codes = load_source_codes(session, args.source_sheet_id, args.source_gid)
    if not source_codes:
        print("Не удалось загрузить коды ТРУ из исходной таблицы.", file=sys.stderr)
        return 1

    processed_codes = load_checkpoint(checkpoint_path)
    existing_keys = load_existing_keys(output_csv)
    fh, writer = ensure_output_writer(output_csv)

    total_codes = 0
    processed_now = 0
    added_rows = 0
    errors = 0

    try:
        for code, product_name in iter_codes(source_codes, args.max_codes):
            total_codes += 1
            if code in processed_codes:
                continue

            print(f"[{total_codes}] Код ТРУ: {code}")
            try:
                first_html = fetch_page_html(
                    session=session,
                    code=code,
                    page=1,
                    year=args.year,
                    status=args.status,
                    amount_from=args.amount_from,
                    timeout=args.timeout,
                )
                soup = BeautifulSoup(first_html, "html5lib")
                max_page = parse_max_page(soup)
                rows = parse_lot_rows(soup, code=code, product_name=product_name)

                for page in range(2, max_page + 1):
                    html = fetch_page_html(
                        session=session,
                        code=code,
                        page=page,
                        year=args.year,
                        status=args.status,
                        amount_from=args.amount_from,
                        timeout=args.timeout,
                    )
                    page_soup = BeautifulSoup(html, "html5lib")
                    rows.extend(parse_lot_rows(page_soup, code=code, product_name=product_name))
                    time.sleep(random.uniform(0.1, 0.3))

                code_added = 0
                for row in rows:
                    key = (row["№ лота"], row["Код ТРУ"])
                    if key in existing_keys:
                        continue
                    writer.writerow(row)
                    existing_keys.add(key)
                    code_added += 1

                fh.flush()
                processed_codes.add(code)
                save_checkpoint(checkpoint_path, sorted(processed_codes))

                processed_now += 1
                added_rows += code_added
                print(
                    f"  -> страниц: {max_page}, найдено: {len(rows)}, "
                    f"добавлено: {code_added}, всего добавлено: {added_rows}"
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  !! ошибка для кода {code}: {exc}", file=sys.stderr)
                # Код не помечаем обработанным, чтобы повторить на следующем запуске.
                continue
    finally:
        fh.close()

    print("\nГотово.")
    print(f"Всего кодов в очереди: {total_codes}")
    print(f"Обработано в этом запуске: {processed_now}")
    print(f"Добавлено строк: {added_rows}")
    print(f"Ошибок: {errors}")
    print(f"CSV: {output_csv}")
    print(f"Checkpoint: {checkpoint_path}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
