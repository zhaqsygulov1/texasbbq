#!/usr/bin/env python3
import argparse
import csv
import io
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"

OUTPUT_HEADER = [
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

TOTAL_RE = re.compile(r"Показано\s*c\s*\d+\s*по\s*\d+\s*из\s*([0-9\s]+)\s*записей")

THREAD_LOCAL = threading.local()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сбор лотов goszakup по кодам ТРУ из Google Sheet за 2025 год "
            "(status=360, amount_from>=15000000)."
        )
    )
    parser.add_argument(
        "--source-sheet-id",
        default=SOURCE_SHEET_ID,
        help="Google Sheet ID со списком кодов ТРУ (колонки: Код ТРУ, Название).",
    )
    parser.add_argument(
        "--target-sheet-id",
        default=TARGET_SHEET_ID,
        help="Google Sheet ID для загрузки результата (используется с --upload).",
    )
    parser.add_argument(
        "--output-csv",
        default="output/lots_2025_tru.csv",
        help="Локальный CSV-файл результата.",
    )
    parser.add_argument(
        "--state-json",
        default="output/lots_2025_state.json",
        help="Файл состояния для возобновления.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Количество параллельных потоков для кодов ТРУ.",
    )
    parser.add_argument(
        "--count-record",
        type=int,
        default=500,
        help="Размер страницы в поиске goszakup (рекомендуется 500 для стабильности).",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=0,
        help="Ограничить число кодов для теста (0 = все коды).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Возобновить run из state-json/output-csv.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Загрузить итог в target Google Sheet (требуются учетные данные).",
    )
    parser.add_argument(
        "--credentials-json",
        default="",
        help=(
            "Путь до service-account credentials JSON для Google Sheets "
            "(если не указан, берется GOOGLE_APPLICATION_CREDENTIALS)."
        ),
    )
    return parser.parse_args()


def get_session() -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is not None:
        return session

    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    THREAD_LOCAL.session = session
    return session


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def csv_export_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"


def load_source_codes(sheet_id: str) -> List[Tuple[str, str]]:
    session = get_session()
    response = session.get(csv_export_url(sheet_id), timeout=120)
    response.raise_for_status()
    response.encoding = "utf-8"

    data = list(csv.reader(io.StringIO(response.text)))
    if not data:
        raise RuntimeError("Исходная таблица кодов ТРУ пустая.")

    result: List[Tuple[str, str]] = []
    for row in data[1:]:
        if not row:
            continue
        code = normalize_text(row[0] if len(row) > 0 else "")
        name = normalize_text(row[1] if len(row) > 1 else "")
        if code:
            result.append((code, name))
    return result


def parse_total_records(raw_html: str, soup: BeautifulSoup, row_count: int) -> int:
    text = raw_html.replace("\xa0", " ")
    match = TOTAL_RE.search(text)
    if match:
        return int(match.group(1).replace(" ", ""))
    return row_count


def parse_rows(soup: BeautifulSoup) -> List[List[str]]:
    table = soup.select_one("#search-result")
    if table is None:
        return []

    parsed: List[List[str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        cells = [normalize_text(td.get_text(" ", strip=True)) for td in tds[:7]]
        if cells[2].endswith(" История"):
            cells[2] = cells[2][: -len(" История")].strip()
        parsed.append(cells)
    return parsed


def fetch_page(
    code: str,
    page: int,
    count_record: int,
) -> Tuple[List[List[str]], int]:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": code,
        "filter[status][]": "360",
        "filter[customer]": "",
        "filter[amount_from]": "15000000",
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": "2025",
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": str(count_record),
        "page": str(page),
    }

    session = get_session()
    last_error: Optional[Exception] = None
    for attempt in range(1, 7):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=60)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            rows = parse_rows(soup)
            total = parse_total_records(response.text, soup, len(rows))
            return rows, total
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            sleep_s = min(20.0, attempt * 1.5 + random.random())
            time.sleep(sleep_s)
    raise RuntimeError(f"{code}: page={page} request failed after retries: {last_error}")


def fetch_code_lots(code: str, product_name: str, count_record: int) -> List[List[str]]:
    page = 1
    total_records = None
    rows_out: List[List[str]] = []

    while True:
        rows, total = fetch_page(code=code, page=page, count_record=count_record)
        if total_records is None:
            total_records = total

        if not rows:
            break

        for cells in rows:
            rows_out.append(
                [
                    cells[0],  # № лота
                    code,  # Код ТРУ
                    product_name,  # Наименование товара (из source table)
                    cells[1],  # Наименование объявления
                    cells[2],  # Наименование и описание лота
                    cells[3],  # Кол-во
                    cells[4],  # Сумма, тг.
                    cells[5],  # Способ закупки
                    cells[6],  # Статус
                ]
            )

        fetched = page * count_record
        if len(rows) < count_record:
            break
        if total_records is not None and total_records > 0 and fetched >= total_records:
            break
        if page >= 200:
            break

        page += 1
        time.sleep(0.15 + random.random() * 0.15)

    return rows_out


def load_csv_keys(path: Path) -> Dict[str, None]:
    keys: Dict[str, None] = {}
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        _ = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            key = f"{row[0]}||{row[1]}"
            keys[key] = None
    return keys


def load_state(path: Path) -> Dict[str, List[str]]:
    if not path.exists():
        return {"processed_codes": [], "failed_codes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: Dict[str, List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def upload_to_google_sheet(
    target_sheet_id: str,
    csv_path: Path,
    credentials_json: str,
) -> None:
    if not credentials_json:
        raise RuntimeError(
            "Не передан --credentials-json и не задан GOOGLE_APPLICATION_CREDENTIALS."
        )

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Для --upload установите зависимости: pip install gspread google-auth"
        ) from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_json, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(target_sheet_id)
    worksheet = spreadsheet.sheet1

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    worksheet.clear()
    chunk = 3000
    start = 1
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        end = start + len(part) - 1
        worksheet.update(f"A{start}:I{end}", part, value_input_option="RAW")
        start = end + 1


def main() -> None:
    args = parse_args()

    output_path = Path(args.output_csv)
    state_path = Path(args.state_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source_codes = load_source_codes(args.source_sheet_id)
    if args.max_codes > 0:
        source_codes = source_codes[: args.max_codes]

    state = load_state(state_path) if args.resume else {"processed_codes": [], "failed_codes": []}
    processed_codes = set(state.get("processed_codes", []))
    failed_codes = set(state.get("failed_codes", []))

    existing_keys: Dict[str, None] = {}
    need_write_header = True
    if args.resume and output_path.exists():
        existing_keys = load_csv_keys(output_path)
        need_write_header = False

    mode = "a" if output_path.exists() and args.resume else "w"
    with output_path.open(mode, encoding="utf-8-sig", newline="") as out_f:
        writer = csv.writer(out_f)
        if need_write_header:
            writer.writerow(OUTPUT_HEADER)
            out_f.flush()

        todo = [(code, name) for code, name in source_codes if code not in processed_codes]
        total_codes = len(source_codes)
        done = len(processed_codes)
        print(f"Всего кодов: {total_codes}. К обработке в этом запуске: {len(todo)}.")

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(fetch_code_lots, code, name, args.count_record): (code, name)
                for code, name in todo
            }

            for future in as_completed(futures):
                code, name = futures[future]
                try:
                    rows = future.result()
                    new_rows = 0
                    for row in rows:
                        key = f"{row[0]}||{row[1]}"
                        if key in existing_keys:
                            continue
                        writer.writerow(row)
                        existing_keys[key] = None
                        new_rows += 1
                    out_f.flush()
                    processed_codes.add(code)
                    failed_codes.discard(code)
                    done += 1
                    print(
                        f"[{done}/{total_codes}] {code}: fetched={len(rows)} written={new_rows}"
                    )
                except Exception as exc:  # noqa: BLE001
                    failed_codes.add(code)
                    print(f"[{done}/{total_codes}] {code}: FAILED -> {exc}")

                state = {
                    "processed_codes": sorted(processed_codes),
                    "failed_codes": sorted(failed_codes),
                }
                save_state(state_path, state)

        if failed_codes:
            print(f"\nПовторная попытка для {len(failed_codes)} failed codes (последовательно).")
            for code in sorted(list(failed_codes)):
                name = next((n for c, n in source_codes if c == code), "")
                try:
                    rows = fetch_code_lots(code, name, args.count_record)
                    new_rows = 0
                    for row in rows:
                        key = f"{row[0]}||{row[1]}"
                        if key in existing_keys:
                            continue
                        writer.writerow(row)
                        existing_keys[key] = None
                        new_rows += 1
                    out_f.flush()
                    processed_codes.add(code)
                    failed_codes.discard(code)
                    done += 1
                    print(
                        f"[retry] {code}: fetched={len(rows)} written={new_rows} -> OK"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[retry] {code}: still FAILED -> {exc}")

                state = {
                    "processed_codes": sorted(processed_codes),
                    "failed_codes": sorted(failed_codes),
                }
                save_state(state_path, state)

    print("\nГотово.")
    print(f"CSV: {output_path}")
    print(f"STATE: {state_path}")
    print(f"Unique lot+code keys: {len(existing_keys)}")
    if failed_codes:
        print(f"Остались ошибки по кодам: {len(failed_codes)}")

    if args.upload:
        credentials_path = args.credentials_json or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS", ""
        )
        if not credentials_path:
            raise RuntimeError(
                "Для --upload укажите --credentials-json или env GOOGLE_APPLICATION_CREDENTIALS."
            )
        upload_to_google_sheet(
            target_sheet_id=args.target_sheet_id,
            csv_path=output_path,
            credentials_json=credentials_path,
        )
        print(f"Данные загружены в Google Sheet: {args.target_sheet_id}")


if __name__ == "__main__":
    main()
