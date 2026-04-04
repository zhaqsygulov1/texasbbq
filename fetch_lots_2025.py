#!/usr/bin/env python3
import argparse
import csv
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_GID = "0"
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

CODE_RE = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")
LOT_RE = re.compile(r"\d{6,}-[0-9A-ZА-ЯЁ-]+")
SPACES_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    return SPACES_RE.sub(" ", value).strip()


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_source_codes(session: requests.Session, sheet_id: str, gid: str) -> List[Tuple[str, str]]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    resp = session.get(url, params={"format": "csv", "gid": gid}, timeout=60)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise RuntimeError("Исходная таблица кодов пустая.")

    result: List[Tuple[str, str]] = []
    for row in rows[1:]:
        if not row:
            continue
        code = normalize_text(row[0]) if len(row) > 0 else ""
        name = normalize_text(row[1]) if len(row) > 1 else ""
        if CODE_RE.match(code):
            result.append((code, name))
    return result


def parse_total_pages(soup: BeautifulSoup) -> int:
    pages = [1]
    for a_tag in soup.select("ul.pagination a[href*='page=']"):
        href = a_tag.get("href") or ""
        query = parse_qs(urlparse(href).query)
        page_values = query.get("page", [])
        for value in page_values:
            if value.isdigit():
                pages.append(int(value))
    return max(pages)


def parse_row_cells(cells) -> Optional[Dict[str, str]]:
    if len(cells) < 7:
        return None

    lot_number = ""
    for strong in cells[0].find_all("strong"):
        candidate = normalize_text(strong.get_text(" ", strip=True))
        if LOT_RE.fullmatch(candidate):
            lot_number = candidate
            break
    if not lot_number:
        match = LOT_RE.search(normalize_text(cells[0].get_text(" ", strip=True)))
        if match:
            lot_number = match.group(0)
    announce_name = ""
    announce_strong = cells[1].select_one("a strong")
    if announce_strong:
        announce_name = normalize_text(announce_strong.get_text(" ", strip=True))
    if not announce_name:
        announce_name = normalize_text(cells[1].get_text(" ", strip=True))

    lot_name = ""
    lot_strong = cells[2].select_one("a strong")
    if lot_strong:
        lot_name = normalize_text(lot_strong.get_text(" ", strip=True))
    if not lot_name:
        lot_name = normalize_text(cells[2].get_text(" ", strip=True))

    qty = normalize_text(cells[3].get_text(" ", strip=True))
    amount = normalize_text(cells[4].get_text(" ", strip=True))
    method = normalize_text(cells[5].get_text(" ", strip=True))
    status = normalize_text(cells[6].get_text(" ", strip=True))

    if not lot_number:
        return None

    return {
        "№ лота": lot_number,
        "Наименование объявления": announce_name,
        "Наименование и описание лота": lot_name,
        "Кол-во": qty,
        "Сумма, тг.": amount,
        "Способ закупки": method,
        "Статус": status,
    }


def parse_lots_page(html: str) -> Tuple[List[Dict[str, str]], int]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#search-result")
    if table is None:
        return [], 1

    rows = []
    tbody = table.find("tbody")
    if tbody is not None:
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            parsed = parse_row_cells(cells)
            if parsed:
                rows.append(parsed)
    total_pages = parse_total_pages(soup)
    return rows, total_pages


def fetch_lots_page(
    session: requests.Session,
    code: str,
    year: int,
    page: int,
    count_record: int,
    status_filter: Optional[List[str]],
    amount_from: Optional[str],
) -> str:
    params = {
        "filter[enstru]": code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
    }
    if amount_from:
        params["filter[amount_from]"] = amount_from
    if status_filter:
        for idx, status in enumerate(status_filter):
            params[f"filter[status][{idx}]"] = status
    response = session.get(SEARCH_URL, params=params, timeout=60)
    response.raise_for_status()
    return response.text


def load_checkpoint(path: Path) -> Dict:
    if not path.exists():
        return {"next_index": 0, "rows_written": 0}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if "next_index" not in data:
        data["next_index"] = 0
    if "rows_written" not in data:
        data["rows_written"] = 0
    return data


def save_checkpoint(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_header_if_needed(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_HEADERS)
        writer.writeheader()


def append_rows(path: Path, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_HEADERS)
        writer.writerows(rows)


def parse_status_filter(value: str) -> List[str]:
    parts = [x.strip() for x in value.split(",")]
    return [x for x in parts if x]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сбор лотов goszakup за год по списку кодов ТРУ из Google Sheets."
    )
    parser.add_argument("--year", type=int, default=2025, help="Год закупок.")
    parser.add_argument("--sheet-id", default=SOURCE_SHEET_ID, help="ID таблицы с кодами ТРУ.")
    parser.add_argument("--sheet-gid", default=SOURCE_GID, help="GID листа в таблице с кодами.")
    parser.add_argument(
        "--output",
        default="output/lots_2025_by_tru.csv",
        help="Путь к итоговому CSV.",
    )
    parser.add_argument(
        "--checkpoint",
        default="output/lots_2025_checkpoint.json",
        help="Путь к JSON чекпоинту.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Продолжить с чекпоинта, иначе начать заново.",
    )
    parser.add_argument(
        "--status",
        default="",
        help="Фильтр статуса(ов), через запятую. Пример: 360",
    )
    parser.add_argument(
        "--amount-from",
        default="",
        help="Минимальная сумма фильтра filter[amount_from].",
    )
    parser.add_argument(
        "--count-record",
        type=int,
        default=50,
        help="Количество записей на странице поиска.",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Ограничить количество кодов для прогона (0 = без ограничений).",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.25,
        help="Пауза (сек) между HTTP запросами.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint)
    status_filter = parse_status_filter(args.status)
    amount_from = args.amount_from.strip()

    session = build_session()
    codes = fetch_source_codes(session, args.sheet_id, args.sheet_gid)
    if args.limit_codes > 0:
        codes = codes[: args.limit_codes]

    if not codes:
        print("Нет валидных кодов ТРУ в исходной таблице.")
        return 1

    checkpoint = {"next_index": 0, "rows_written": 0}
    if args.resume:
        checkpoint = load_checkpoint(checkpoint_path)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        if checkpoint_path.exists():
            checkpoint_path.unlink()

    write_header_if_needed(output_path)

    start_index = checkpoint.get("next_index", 0)
    rows_written = checkpoint.get("rows_written", 0)
    total_codes = len(codes)
    print(f"Кодов для обработки: {total_codes}. Стартовый индекс: {start_index}.")

    for idx in range(start_index, total_codes):
        code, product_name = codes[idx]

        try:
            first_html = fetch_lots_page(
                session=session,
                code=code,
                year=args.year,
                page=1,
                count_record=args.count_record,
                status_filter=status_filter,
                amount_from=amount_from if amount_from else None,
            )
        except Exception as err:
            print(f"[{idx+1}/{total_codes}] Ошибка запроса для кода {code}: {err}", file=sys.stderr)
            checkpoint["next_index"] = idx
            checkpoint["rows_written"] = rows_written
            save_checkpoint(checkpoint_path, checkpoint)
            return 2

        page_rows, total_pages = parse_lots_page(first_html)
        full_rows: List[Dict[str, str]] = []
        for row in page_rows:
            row["Код ТРУ"] = code
            row["Наименование товара"] = product_name
            full_rows.append(row)

        if total_pages > 1:
            for page in range(2, total_pages + 1):
                if args.pause > 0:
                    time.sleep(args.pause)
                try:
                    html = fetch_lots_page(
                        session=session,
                        code=code,
                        year=args.year,
                        page=page,
                        count_record=args.count_record,
                        status_filter=status_filter,
                        amount_from=amount_from if amount_from else None,
                    )
                except Exception as err:
                    print(
                        f"[{idx+1}/{total_codes}] Ошибка на странице {page}/{total_pages} "
                        f"для кода {code}: {err}",
                        file=sys.stderr,
                    )
                    checkpoint["next_index"] = idx
                    checkpoint["rows_written"] = rows_written
                    save_checkpoint(checkpoint_path, checkpoint)
                    return 2

                rows, _ = parse_lots_page(html)
                for row in rows:
                    row["Код ТРУ"] = code
                    row["Наименование товара"] = product_name
                    full_rows.append(row)

        append_rows(output_path, full_rows)
        rows_written += len(full_rows)
        checkpoint["next_index"] = idx + 1
        checkpoint["rows_written"] = rows_written
        save_checkpoint(checkpoint_path, checkpoint)
        print(
            f"[{idx+1}/{total_codes}] Код {code}: страниц={total_pages}, "
            f"лотов={len(full_rows)}, всего_лотов={rows_written}"
        )

        if args.pause > 0:
            time.sleep(args.pause)

    print(
        f"Готово. Обработано кодов: {total_codes}. "
        f"Итоговых лотов: {rows_written}. Файл: {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
