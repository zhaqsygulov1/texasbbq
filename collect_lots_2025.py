#!/usr/bin/env python3
import argparse
import csv
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
SOURCE_GID = "0"
DEFAULT_YEAR = "2025"
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

OUT_COLUMNS = [
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


@dataclass
class TruCode:
    code: str
    name: str


def source_csv_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


def request_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, str],
    *,
    retries: int = 5,
    timeout: int = 40,
) -> str:
    delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            if attempt == retries:
                raise RuntimeError(f"Failed request after {retries} attempts: {exc}") from exc
            time.sleep(delay)
            delay = min(delay * 2, 10)
    raise RuntimeError("Unexpected retry loop termination")


def load_tru_codes(session: requests.Session, sheet_id: str, gid: str) -> list[TruCode]:
    response = session.get(source_csv_url(sheet_id, gid), timeout=60)
    response.raise_for_status()
    lines = response.text.splitlines()
    reader = csv.DictReader(lines)
    result: list[TruCode] = []
    for row in reader:
        code = normalize_text(row.get("Код ТРУ", ""))
        name = normalize_text(row.get("Название", ""))
        if not code:
            continue
        result.append(TruCode(code=code, name=name))
    return result


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for link in soup.select("ul.pagination a[href*='page=']"):
        href = link.get("href") or ""
        parsed = parse_qs(urlparse(href).query)
        page_values = parsed.get("page", [])
        if not page_values:
            continue
        try:
            page_no = int(page_values[0])
        except ValueError:
            continue
        max_page = max(max_page, page_no)
    return max_page


def parse_rows_from_html(html_text: str, tru_code: TruCode) -> tuple[list[dict[str, str]], int]:
    soup = BeautifulSoup(html_text, "lxml")
    table = soup.select_one("#search-result")
    if not table:
        return [], 1

    rows_data: list[dict[str, str]] = []
    body_rows = table.select("tbody tr")
    for tr in body_rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        lot_number = normalize_text((tds[0].find("strong") or tds[0]).get_text(" ", strip=True))
        announce_name = normalize_text(
            ((tds[1].find("a").find("strong") if tds[1].find("a") else None) or tds[1]).get_text(
                " ", strip=True
            )
        )

        for history in tds[2].select(".btn-select-history, small"):
            history.extract()
        lot_name_desc = normalize_text(tds[2].get_text(" ", strip=True))

        qty = normalize_text(tds[3].get_text(" ", strip=True))
        amount = normalize_text(tds[4].get_text(" ", strip=True))
        trade_type = normalize_text(tds[5].get_text(" ", strip=True))
        status = normalize_text(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        rows_data.append(
            {
                "№ лота": lot_number,
                "Код ТРУ": tru_code.code,
                "Наименование товара": tru_code.name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_name_desc,
                "Кол-во": qty,
                "Сумма, тг.": amount,
                "Способ закупки": trade_type,
                "Статус": status,
            }
        )
    return rows_data, parse_max_page(soup)


def iter_lots_for_code(
    session: requests.Session,
    tru_code: TruCode,
    year: str,
    statuses: list[str],
) -> Iterable[dict[str, str]]:
    params = {
        "filter[enstru]": tru_code.code,
        "filter[year]": year,
        "count_record": "50",
    }
    if statuses:
        for idx, status in enumerate(statuses):
            params[f"filter[status][{idx}]"] = status

    page = 1
    max_page = 1
    while page <= max_page:
        params["page"] = str(page)
        html_text = request_with_retry(session, BASE_URL, params)
        rows, detected_max_page = parse_rows_from_html(html_text, tru_code)
        max_page = max(max_page, detected_max_page)
        for row in rows:
            yield row
        page += 1
        time.sleep(0.2)


def collect_rows_for_code(
    tru_code: TruCode,
    year: str,
    statuses: list[str],
) -> tuple[TruCode, list[dict[str, str]], str | None]:
    session = make_session()
    rows: list[dict[str, str]] = []
    err: str | None = None
    try:
        for row in iter_lots_for_code(session, tru_code, year, statuses):
            rows.append(row)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    finally:
        session.close()
    return tru_code, rows, err


def parse_statuses(statuses_raw: str) -> list[str]:
    statuses_raw = statuses_raw.strip()
    if not statuses_raw:
        return []
    return [s.strip() for s in statuses_raw.split(",") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Собирает лоты goszakup за год по списку кодов ТРУ из Google Sheet "
            "и сохраняет в CSV c колонками под итоговую таблицу."
        )
    )
    parser.add_argument("--year", default=DEFAULT_YEAR, help="Год поиска (по умолчанию 2025)")
    parser.add_argument(
        "--statuses",
        default="360",
        help=(
            "Статусы через запятую для filter[status]. "
            "Пустая строка = без фильтра по статусу. По умолчанию: 360"
        ),
    )
    parser.add_argument(
        "--source-sheet-id",
        default=SOURCE_SHEET_ID,
        help="ID Google Sheet со списком кодов ТРУ",
    )
    parser.add_argument("--source-gid", default=SOURCE_GID, help="GID листа с кодами ТРУ")
    parser.add_argument(
        "--output",
        default="lots_2025_filtered.csv",
        help="Файл вывода CSV (UTF-8 with BOM)",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Ограничить число кодов (для теста). 0 = все коды.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Количество параллельных потоков обработки кодов ТРУ.",
    )
    args = parser.parse_args()

    statuses = parse_statuses(args.statuses)
    session = make_session()

    print("Загружаю список кодов ТРУ...", flush=True)
    codes = load_tru_codes(session, args.source_sheet_id, args.source_gid)
    if args.limit_codes and args.limit_codes > 0:
        codes = codes[: args.limit_codes]
    print(f"Кодов к обработке: {len(codes)}", flush=True)

    total_rows = 0
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        writer.writeheader()

        if args.workers <= 1:
            for idx, code in enumerate(codes, start=1):
                per_code = 0
                try:
                    for row in iter_lots_for_code(session, code, args.year, statuses):
                        writer.writerow(row)
                        per_code += 1
                        total_rows += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] Код {code.code}: ошибка ({exc})", file=sys.stderr)

                if idx % 10 == 0 or idx == len(codes):
                    print(
                        f"Обработано кодов: {idx}/{len(codes)} | "
                        f"лотов по текущему коду: {per_code} | всего лотов: {total_rows}",
                        flush=True,
                    )
        else:
            completed = 0
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [
                    executor.submit(collect_rows_for_code, code, args.year, statuses) for code in codes
                ]
                for future in as_completed(futures):
                    code, rows, err = future.result()
                    for row in rows:
                        writer.writerow(row)
                    total_rows += len(rows)
                    completed += 1
                    if err:
                        print(f"[WARN] Код {code.code}: ошибка ({err})", file=sys.stderr)
                    if completed % 10 == 0 or completed == len(codes):
                        print(
                            f"Обработано кодов: {completed}/{len(codes)} | "
                            f"лотов по последнему коду: {len(rows)} | всего лотов: {total_rows}",
                            flush=True,
                        )

    print(f"Готово. Файл: {args.output} | строк: {total_rows}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
