#!/usr/bin/env python3
import argparse
import csv
import io
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
DEFAULT_SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)

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

TOTAL_RE = re.compile(r"Показано c\s+\d+\s+по\s+\d+\s+из\s+(\d+)\s+записей")
THREAD_LOCAL = threading.local()


def normalize_text(value: str) -> str:
    value = (value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def build_url(
    *,
    code: str,
    year: int,
    page: Optional[int],
    count_record: int,
    status: Optional[str],
    amount_from: Optional[str],
) -> str:
    params: List[Tuple[str, str]] = [
        ("filter[name]", ""),
        ("filter[number]", ""),
        ("filter[number_anno]", ""),
        ("filter[enstru]", code),
        ("filter[customer]", ""),
        ("filter[amount_from]", amount_from or ""),
        ("filter[amount_to]", ""),
        ("filter[trade_type]", ""),
        ("filter[month]", ""),
        ("filter[plan_number]", ""),
        ("filter[end_date_from]", ""),
        ("filter[end_date_to]", ""),
        ("filter[start_date_to]", ""),
        ("filter[year]", str(year)),
        ("filter[itogi_date_from]", ""),
        ("filter[itogi_date_to]", ""),
        ("filter[start_date_from]", ""),
        ("filter[more]", ""),
        ("count_record", str(count_record)),
        ("smb", ""),
    ]
    if status:
        params.append(("filter[status][]", status))
    if page and page > 1:
        params.append(("page", str(page)))
    return f"{BASE_URL}?{urlencode(params, doseq=True)}"


def get_session() -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is not None:
        return session

    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        backoff_factor=0.3,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    THREAD_LOCAL.session = session
    return session


def fetch_html(url: str, timeout: int, max_retries: int) -> str:
    session = get_session()
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, timeout=(15, timeout))
            if response.status_code >= 400:
                raise requests.HTTPError(
                    f"HTTP {response.status_code} for {url}", response=response
                )
            return response.text
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt == max_retries:
                break
            sleep_seconds = min(8, (2 ** (attempt - 1)) + random.random())
            time.sleep(sleep_seconds)
    if last_err is None:
        raise RuntimeError(f"Unknown fetch error for {url}")
    raise RuntimeError(f"Failed to fetch after retries: {url}") from last_err


def extract_lot_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [normalize_text(th.get_text(" ", strip=True)) for th in table.select("th")]
        if headers[:2] == ["№ лота", "Наименование объявления"]:
            return table
    return None


def parse_total_records(soup: BeautifulSoup) -> int:
    text = soup.get_text(" ", strip=True)
    match = TOTAL_RE.search(text)
    if not match:
        return 0
    return int(match.group(1))


def cell_anchor_or_text(cell, remove_after: Optional[str] = None) -> str:
    anchor = cell.find("a")
    text = anchor.get_text(" ", strip=True) if anchor else cell.get_text(" ", strip=True)
    text = normalize_text(text)
    if remove_after and remove_after in text:
        text = text.split(remove_after, 1)[0].strip()
    return text


def parse_page_rows(html: str, code: str, product_name: str) -> Tuple[int, List[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    total = parse_total_records(soup)
    table = extract_lot_table(soup)
    if table is None:
        return total, []

    rows: List[Dict[str, str]] = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue

        lot_number = normalize_text(cells[0].get_text(" ", strip=True))
        if not lot_number:
            continue

        rows.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": code,
                "Наименование товара": product_name,
                "Наименование объявления": cell_anchor_or_text(cells[1], remove_after="Заказчик:"),
                "Наименование и описание лота": cell_anchor_or_text(cells[2]),
                "Кол-во": normalize_text(cells[3].get_text(" ", strip=True)),
                "Сумма, тг.": normalize_text(cells[4].get_text(" ", strip=True)),
                "Способ закупки": normalize_text(cells[5].get_text(" ", strip=True)),
                "Статус": normalize_text(cells[6].get_text(" ", strip=True)),
            }
        )
    return total, rows


def fetch_code_rows(
    *,
    code: str,
    product_name: str,
    year: int,
    status: Optional[str],
    amount_from: Optional[str],
    count_record: int,
    timeout: int,
    max_retries: int,
) -> Tuple[List[Dict[str, str]], Dict[str, object]]:
    page_1_url = build_url(
        code=code,
        year=year,
        page=None,
        count_record=count_record,
        status=status,
        amount_from=amount_from,
    )
    first_html = fetch_html(page_1_url, timeout=timeout, max_retries=max_retries)
    total, rows = parse_page_rows(first_html, code, product_name)

    if total <= count_record:
        return rows, {"code": code, "total": total, "pages": 1, "cap_10000": total == 10000}

    total_pages = math.ceil(total / count_record)
    all_rows = list(rows)
    for page in range(2, total_pages + 1):
        url = build_url(
            code=code,
            year=year,
            page=page,
            count_record=count_record,
            status=status,
            amount_from=amount_from,
        )
        html = fetch_html(url, timeout=timeout, max_retries=max_retries)
        _, page_rows = parse_page_rows(html, code, product_name)
        all_rows.extend(page_rows)

    return all_rows, {
        "code": code,
        "total": total,
        "pages": total_pages,
        "cap_10000": total == 10000,
    }


def load_source_codes(source_url: str) -> List[Tuple[str, str]]:
    resp = requests.get(source_url, timeout=60)
    resp.raise_for_status()
    csv_text = resp.content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(csv_text))
    field_map = {normalize_text(name).lower(): name for name in (reader.fieldnames or [])}
    code_field = field_map.get("код тру")
    name_field = field_map.get("название")
    if not code_field:
        raise RuntimeError(
            f"Не найден столбец 'Код ТРУ' в source CSV. headers={reader.fieldnames}"
        )

    result: List[Tuple[str, str]] = []
    for row in reader:
        code = normalize_text(row.get(code_field, ""))
        title = normalize_text(row.get(name_field, "")) if name_field else ""
        if code:
            result.append((code, title))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сбор лотов goszakup по кодам ТРУ за 2025 год."
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument(
        "--source-csv",
        default="",
        help="Локальный CSV-файл с колонками 'Код ТРУ' и 'Название'. Если задан, source-url игнорируется.",
    )
    parser.add_argument("--output-csv", default="output/lots_2025.csv")
    parser.add_argument("--warnings-json", default="output/lots_2025_warnings.json")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument(
        "--status",
        default="360",
        help="Код статуса на goszakup (360 = Закупка состоялась). Пусто для всех статусов.",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Минимальная сумма закупки, пусто для без фильтра.",
    )
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=80)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--limit-codes", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.source_csv:
        csv_text = Path(args.source_csv).read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(csv_text))
        field_map = {normalize_text(name).lower(): name for name in (reader.fieldnames or [])}
        code_field = field_map.get("код тру")
        name_field = field_map.get("название")
        if not code_field:
            raise RuntimeError(
                f"Не найден столбец 'Код ТРУ' в source CSV. headers={reader.fieldnames}"
            )
        codes = []
        for row in reader:
            code = normalize_text(row.get(code_field, ""))
            title = normalize_text(row.get(name_field, "")) if name_field else ""
            if code:
                codes.append((code, title))
    else:
        codes = load_source_codes(args.source_url)
    if args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    warnings_path = Path(args.warnings_json)
    warnings_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Кодов ТРУ для обработки: {len(codes)}")
    print(
        f"Фильтры: year={args.year}, status={args.status or 'ALL'}, "
        f"amount_from={args.amount_from or 'ALL'}, count_record={args.count_record}"
    )

    completed = 0
    total_rows_written = 0
    errors: List[Dict[str, str]] = []
    capped_codes: List[str] = []
    seen_keys = set()

    with output_path.open("w", encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=OUTPUT_HEADERS)
        writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_to_code = {
                pool.submit(
                    fetch_code_rows,
                    code=code,
                    product_name=name,
                    year=args.year,
                    status=(args.status or None),
                    amount_from=(args.amount_from or None),
                    count_record=args.count_record,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                ): (idx, code)
                for idx, (code, name) in enumerate(codes, start=1)
            }

            for future in as_completed(future_to_code):
                idx, code = future_to_code[future]
                try:
                    rows, meta = future.result()
                    if meta.get("cap_10000"):
                        capped_codes.append(code)

                    written_for_code = 0
                    for row in rows:
                        key = (row["№ лота"], row["Код ТРУ"])
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        writer.writerow(row)
                        written_for_code += 1
                    total_rows_written += written_for_code
                except Exception as err:  # noqa: BLE001
                    errors.append({"code": code, "error": str(err)})

                completed += 1
                if completed % 25 == 0 or completed == len(codes):
                    print(
                        f"[{completed}/{len(codes)}] "
                        f"rows={total_rows_written} errors={len(errors)}"
                    )
                    out_file.flush()

    warnings_data = {
        "total_codes": len(codes),
        "rows_written": total_rows_written,
        "error_count": len(errors),
        "errors": errors,
        "possible_cap_10000_codes": capped_codes,
    }
    warnings_path.write_text(
        json.dumps(warnings_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nГотово.")
    print(f"CSV: {output_path}")
    print(f"Warnings: {warnings_path}")
    print(f"Строк записано: {total_rows_written}")
    print(f"Ошибок: {len(errors)}")
    if capped_codes:
        print(
            "Внимание: есть коды с total=10000 (возможна отсечка на стороне портала): "
            f"{len(capped_codes)}"
        )


if __name__ == "__main__":
    main()
