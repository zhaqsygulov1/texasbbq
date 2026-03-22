#!/usr/bin/env python3
import argparse
import csv
import io
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import local
from typing import Dict, List, Sequence, Set, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"

SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/gviz/tq?tqx=out:csv"
)
TARGET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/gviz/tq?tqx=out:csv"
)
TARGET_EDIT_URL = (
    f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/edit?usp=sharing"
)

GOSZAKUP_SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
COUNT_RECORD = 2000
RESULTS_RE = re.compile(r"Показано c\s*(\d+)\s*по\s*(\d+)\s*из\s*(\d+)\s*записей")
TARGET_HEADERS = [
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

THREAD_LOCAL = local()


@dataclass
class CodeResult:
    code: str
    rows: List[List[str]]
    total_reported: int
    pages: int
    error: str = ""


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def worker_session() -> requests.Session:
    if not hasattr(THREAD_LOCAL, "session"):
        THREAD_LOCAL.session = build_session()
    return THREAD_LOCAL.session


def download_csv_rows(url: str, timeout: int = 120) -> List[List[str]]:
    text = requests.get(url, timeout=timeout).text
    return list(csv.reader(io.StringIO(text)))


def fetch_source_codes() -> Tuple[Dict[str, str], List[str]]:
    rows = download_csv_rows(SOURCE_CSV_URL)
    if not rows:
        raise RuntimeError("Не удалось прочитать исходную таблицу кодов ТРУ.")

    header = rows[0]
    code_idx = header.index("Код ТРУ")
    name_idx = header.index("Название")

    code_to_name: Dict[str, str] = {}
    ordered_codes: List[str] = []
    for row in rows[1:]:
        if len(row) <= max(code_idx, name_idx):
            continue
        code = row[code_idx].strip()
        if not code:
            continue
        name = row[name_idx].strip()
        if code not in code_to_name:
            ordered_codes.append(code)
        code_to_name[code] = name

    return code_to_name, ordered_codes


def fetch_target_rows() -> Tuple[List[List[str]], Set[str], Set[Tuple[str, str]]]:
    rows = download_csv_rows(TARGET_CSV_URL)
    if not rows:
        raise RuntimeError("Не удалось прочитать итоговую таблицу.")

    header = [h.strip() for h in rows[0]]
    if header[: len(TARGET_HEADERS)] != TARGET_HEADERS:
        raise RuntimeError(
            "Заголовки итоговой таблицы отличаются от ожидаемых. "
            f"Ожидалось: {TARGET_HEADERS}; получено: {header}"
        )

    lot_idx = header.index("№ лота")
    code_idx = header.index("Код ТРУ")

    existing_codes: Set[str] = set()
    existing_keys: Set[Tuple[str, str]] = set()
    for row in rows[1:]:
        if len(row) <= max(lot_idx, code_idx):
            continue
        lot = row[lot_idx].strip()
        code = row[code_idx].strip()
        if code:
            existing_codes.add(code)
        if lot and code:
            existing_keys.add((lot, code))

    return rows, existing_codes, existing_keys


def sanitize_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()


def extract_primary_strong(cell) -> str:
    strong = cell.find("strong")
    if strong:
        return sanitize_cell(strong.get_text(" ", strip=True))
    return sanitize_cell(cell.get_text(" ", strip=True))


def parse_lot_rows_from_html(html_text: str, code: str, product_name: str) -> List[List[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    target_table = None

    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if headers[:3] == [
            "№ лота",
            "Наименование объявления",
            "Наименование и описание лота",
        ]:
            target_table = table
            break

    if target_table is None:
        return []

    parsed_rows: List[List[str]] = []
    for tr in target_table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = extract_primary_strong(tds[0])
        announcement_name = extract_primary_strong(tds[1])
        lot_name = extract_primary_strong(tds[2])
        qty = sanitize_cell(tds[3].get_text(" ", strip=True))
        amount = sanitize_cell(tds[4].get_text(" ", strip=True))
        method = sanitize_cell(tds[5].get_text(" ", strip=True))
        status = sanitize_cell(tds[6].get_text(" ", strip=True))

        if not lot_number or not announcement_name:
            continue

        parsed_rows.append(
            [
                lot_number,
                code,
                product_name,
                announcement_name,
                lot_name,
                qty,
                amount,
                method,
                status,
            ]
        )

    return parsed_rows


def get_total_and_page_size(html_text: str) -> Tuple[int, int]:
    match = RESULTS_RE.search(html_text)
    if not match:
        return 0, COUNT_RECORD
    start, end, total = map(int, match.groups())
    if total <= 0:
        return 0, COUNT_RECORD
    page_size = max(1, end - start + 1)
    return total, page_size


def fetch_page_html(
    session: requests.Session, code: str, page: int, timeout: int = 40
) -> str:
    params = {
        "filter[enstru]": code,
        "filter[status][0]": "360",
        "filter[amount_from]": "15000000",
        "filter[year]": "2025",
        "count_record": str(COUNT_RECORD),
        "smb": "",
    }
    if page > 1:
        params["page"] = str(page)

    attempts = 0
    last_error: Exception | None = None
    while attempts < 5:
        attempts += 1
        try:
            response = session.get(GOSZAKUP_SEARCH_URL, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_s = min(6.0, 0.8 * (2 ** (attempts - 1))) + random.uniform(0.0, 0.6)
            time.sleep(sleep_s)

    query = urlencode(params, doseq=True)
    raise RuntimeError(f"Ошибка запроса {GOSZAKUP_SEARCH_URL}?{query}: {last_error}")


def scrape_code(code: str, product_name: str) -> CodeResult:
    session = worker_session()
    try:
        first_html = fetch_page_html(session, code=code, page=1)
    except Exception as exc:  # noqa: BLE001
        return CodeResult(code=code, rows=[], total_reported=0, pages=0, error=str(exc))

    total, page_size = get_total_and_page_size(first_html)
    if total == 0:
        return CodeResult(code=code, rows=[], total_reported=0, pages=0)

    pages = max(1, math.ceil(total / page_size))
    all_rows = parse_lot_rows_from_html(first_html, code=code, product_name=product_name)

    for page in range(2, pages + 1):
        try:
            html_page = fetch_page_html(session, code=code, page=page)
            all_rows.extend(
                parse_lot_rows_from_html(html_page, code=code, product_name=product_name)
            )
        except Exception as exc:  # noqa: BLE001
            return CodeResult(
                code=code,
                rows=all_rows,
                total_reported=total,
                pages=pages,
                error=f"Частичная ошибка на странице {page}: {exc}",
            )
        time.sleep(random.uniform(0.05, 0.2))

    return CodeResult(code=code, rows=all_rows, total_reported=total, pages=pages)


def rows_to_tsv(rows: Sequence[Sequence[str]]) -> str:
    return "\n".join("\t".join(sanitize_cell(str(cell)) for cell in row) for row in rows)


def append_rows_to_google_sheet(rows: List[List[str]], start_row: int, chunk_size: int) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1700, "height": 1000},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        page.goto(TARGET_EDIT_URL, wait_until="domcontentloaded", timeout=180000)
        page.wait_for_timeout(18000)

        written = 0
        while written < len(rows):
            chunk = rows[written : written + chunk_size]
            target_row = start_row + written
            page.fill("#t-name-box", f"A{target_row}")
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
            tsv_payload = rows_to_tsv(chunk)
            clipboard_result = page.evaluate(
                """async (payload) => {
                    try {
                        await navigator.clipboard.writeText(payload);
                        return "ok";
                    } catch (err) {
                        return `error:${err}`;
                    }
                }""",
                tsv_payload,
            )
            if clipboard_result != "ok":
                raise RuntimeError(
                    f"Ошибка записи в буфер обмена перед вставкой строки {target_row}: "
                    f"{clipboard_result}"
                )
            page.keyboard.press("Control+v")
            page.wait_for_timeout(2500)
            written += len(chunk)
            print(
                f"[sheet] Вставлено {written}/{len(rows)} строк "
                f"(последняя строка A{target_row + len(chunk) - 1})"
            )

        # Небольшая пауза, чтобы autosave успел завершиться перед закрытием браузера.
        page.wait_for_timeout(6000)
        context.close()
        browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сбор лотов за 2025 год по кодам ТРУ (статус=Закупка состоялась, "
            "сумма от 15 млн) и дозапись в Google Sheet."
        )
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=250,
        help="Максимум обрабатываемых отсутствующих кодов за запуск.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Смещение в списке отсутствующих кодов перед обработкой.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Количество параллельных потоков для запросов к goszakup.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=180,
        help="Размер чанка вставки в Google Sheet.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только собрать и показать статистику, без записи в целевую таблицу.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.time()

    print("[init] Чтение исходной таблицы кодов ТРУ...")
    code_to_name, source_codes = fetch_source_codes()
    print(f"[init] Кодов в источнике: {len(source_codes)}")

    print("[init] Чтение целевой таблицы...")
    target_rows, existing_codes, existing_keys = fetch_target_rows()
    target_data_rows = len(target_rows) - 1
    print(
        f"[init] Строк в целевой: {target_data_rows}, "
        f"уникальных кодов: {len(existing_codes)}, уникальных ключей лота: {len(existing_keys)}"
    )

    missing_codes = [code for code in source_codes if code not in existing_codes]
    print(f"[init] Кодов, отсутствующих в целевой: {len(missing_codes)}")
    if not missing_codes:
        print("[done] Все коды уже присутствуют в целевой таблице.")
        return 0

    offset = max(0, args.offset)
    limit = max(0, args.max_codes)
    source_batch = source_codes[offset : offset + limit]
    already_present = sum(1 for code in source_batch if code in existing_codes)
    codes_to_process = [code for code in source_batch if code not in existing_codes]
    print(
        f"[run] Обрабатываем {len(codes_to_process)} кодов "
        f"(workers={args.workers}, dry_run={args.dry_run}, offset={offset}, "
        f"already_present_in_batch={already_present})"
    )

    all_new_rows: List[List[str]] = []
    seen_new_keys: Set[Tuple[str, str]] = set()
    hits = 0
    errors: List[str] = []
    completed = 0
    reported_total_rows = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(scrape_code, code, code_to_name.get(code, "")): code
            for code in codes_to_process
        }
        for future in as_completed(futures):
            completed += 1
            result = future.result()
            if result.error:
                errors.append(f"{result.code}: {result.error}")

            if result.total_reported > 0:
                hits += 1
                reported_total_rows += result.total_reported

            for row in result.rows:
                key = (row[0], row[1])
                if key in existing_keys or key in seen_new_keys:
                    continue
                seen_new_keys.add(key)
                all_new_rows.append(row)

            if completed % 25 == 0 or completed == len(codes_to_process):
                print(
                    f"[progress] {completed}/{len(codes_to_process)} кодов, "
                    f"кодов с результатом: {hits}, новые строки: {len(all_new_rows)}, "
                    f"ошибок: {len(errors)}"
                )

    print(
        f"[summary] Проверено кодов: {len(codes_to_process)}, "
        f"с найденными лотами: {hits}, reported_total_rows={reported_total_rows}, "
        f"добавлено новых уникальных строк: {len(all_new_rows)}"
    )
    if errors:
        print("[summary] Примеры ошибок:")
        for err in errors[:10]:
            print(f"  - {err}")

    if args.dry_run:
        print("[done] Dry-run завершен, запись в таблицу не выполнялась.")
        return 0

    if not all_new_rows:
        print("[done] Новых строк для записи нет.")
        return 0

    if target_data_rows == 0:
        start_row = 2
        payload_rows = all_new_rows
    else:
        # Пишем с последней существующей строки: первая строка payload повторяет якорную
        # строку один-в-один, а все последующие добавляются как новые.
        start_row = target_data_rows + 1
        anchor_row = target_rows[-1][: len(TARGET_HEADERS)]
        if len(anchor_row) < len(TARGET_HEADERS):
            anchor_row = anchor_row + [""] * (len(TARGET_HEADERS) - len(anchor_row))
        payload_rows = [anchor_row] + all_new_rows

    print(
        f"[sheet] Старт записи с строки A{start_row} "
        f"(payload={len(payload_rows)} строк, новых={len(all_new_rows)})"
    )
    append_rows_to_google_sheet(payload_rows, start_row=start_row, chunk_size=args.chunk_size)

    # Проверка факта добавления строк.
    updated_rows = download_csv_rows(TARGET_CSV_URL)
    new_total_data_rows = len(updated_rows) - 1
    delta = new_total_data_rows - target_data_rows
    print(
        f"[verify] Было строк: {target_data_rows}, стало: {new_total_data_rows}, "
        f"прирост: {delta}"
    )

    elapsed = time.time() - started
    print(f"[done] Готово за {elapsed:.1f} сек.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
