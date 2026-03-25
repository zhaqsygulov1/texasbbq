#!/usr/bin/env python3
import argparse
import csv
import re
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEST_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SOURCE_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/export?format=csv&gid=0"
DEST_CSV_URL = f"https://docs.google.com/spreadsheets/d/{DEST_SHEET_ID}/export?format=csv&gid=0"
LOTS_URL = "https://goszakup.gov.kz/ru/search/lots"

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


@dataclass(frozen=True)
class TruCode:
    code: str
    name: str


@dataclass
class LotRecord:
    lot_number: str
    tru_code: str
    tru_name: str
    announcement_name: str
    lot_name: str
    quantity: str
    amount: str
    procurement_method: str
    status: str

    def as_row(self) -> List[str]:
        return [
            self.lot_number,
            self.tru_code,
            self.tru_name,
            self.announcement_name,
            self.lot_name,
            self.quantity,
            self.amount,
            self.procurement_method,
            self.status,
        ]


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def read_tru_codes(session: requests.Session) -> List[TruCode]:
    response = session.get(SOURCE_CSV_URL, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8-sig"
    reader = csv.reader(StringIO(response.text))
    rows = list(reader)
    if not rows:
        return []

    out: List[TruCode] = []
    seen: Set[str] = set()
    for row in rows[1:]:
        if not row:
            continue
        code = (row[0] if len(row) > 0 else "").strip()
        name = (row[1] if len(row) > 1 else "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(TruCode(code=code, name=name))
    return out


def parse_results_count(soup: BeautifulSoup) -> int:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*(\d+)\s*записей", text)
    if not match:
        return 0
    value = match.group(1).replace(" ", "")
    try:
        return int(value)
    except ValueError:
        return 0


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_announcement(cell) -> str:
    first_link = cell.find("a")
    if first_link:
        return clean_text(first_link.get_text(" ", strip=True))
    return clean_text(cell.get_text(" ", strip=True))


def parse_lot_name(cell) -> str:
    links = cell.find_all("a")
    if links:
        # First link is the lot title, second usually "История".
        return clean_text(links[0].get_text(" ", strip=True))
    return clean_text(cell.get_text(" ", strip=True))


def parse_lot_rows(soup: BeautifulSoup) -> Iterable[Tuple[str, str, str, str, str, str, str]]:
    table = None
    for candidate in soup.find_all("table"):
        headers = [clean_text(th.get_text(" ", strip=True)) for th in candidate.find_all("th")]
        if "№ лота" in headers and "Наименование объявления" in headers:
            table = candidate
            break
    if table is None:
        return []

    body = table.find("tbody")
    if not body:
        return []

    out: List[Tuple[str, str, str, str, str, str, str]] = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        lot_number = clean_text(tds[0].get_text(" ", strip=True))
        announcement_name = parse_announcement(tds[1])
        lot_name = parse_lot_name(tds[2])
        quantity = clean_text(tds[3].get_text(" ", strip=True))
        amount = clean_text(tds[4].get_text(" ", strip=True))
        procurement_method = clean_text(tds[5].get_text(" ", strip=True))
        status = clean_text(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        out.append(
            (
                lot_number,
                announcement_name,
                lot_name,
                quantity,
                amount,
                procurement_method,
                status,
            )
        )
    return out


def fetch_for_code(
    session: requests.Session,
    tru: TruCode,
    year: int,
    per_page: int,
    delay: float,
    status_id: int,
    amount_from: str,
    amount_to: str,
) -> List[LotRecord]:
    params = {
        "filter[enstru]": tru.code,
        "filter[year]": str(year),
        "filter[status][]": str(status_id),
        "filter[amount_from]": amount_from,
        "filter[amount_to]": amount_to,
        "count_record": str(per_page),
        "smb": "",
    }
    first = session.get(LOTS_URL, params=params, timeout=90)
    first.raise_for_status()
    soup = BeautifulSoup(first.text, "lxml")
    total = parse_results_count(soup)
    if total == 0:
        return []

    rows = list(parse_lot_rows(soup))
    pages = (total + per_page - 1) // per_page
    if pages > 1:
        for page in range(2, pages + 1):
            params["page"] = str(page)
            resp = session.get(LOTS_URL, params=params, timeout=90)
            resp.raise_for_status()
            page_soup = BeautifulSoup(resp.text, "lxml")
            rows.extend(parse_lot_rows(page_soup))
            if delay:
                time.sleep(delay)

    out: List[LotRecord] = []
    for item in rows:
        out.append(
            LotRecord(
                lot_number=item[0],
                tru_code=tru.code,
                tru_name=tru.name,
                announcement_name=item[1],
                lot_name=item[2],
                quantity=item[3],
                amount=item[4],
                procurement_method=item[5],
                status=item[6],
            )
        )
    return out


def fetch_existing_rows(session: requests.Session) -> Set[Tuple[str, str]]:
    """Returns set of (lot_number, tru_code) keys already present in destination sheet."""
    response = session.get(DEST_CSV_URL, timeout=60)
    response.raise_for_status()
    response.encoding = "utf-8-sig"

    reader = csv.reader(StringIO(response.text))
    rows = list(reader)
    if not rows:
        return set()

    header = [clean_text(x) for x in rows[0]]
    lot_idx = header.index("№ лота") if "№ лота" in header else None
    code_idx = header.index("Код ТРУ") if "Код ТРУ" in header else None
    if lot_idx is None or code_idx is None:
        return set()

    keys: Set[Tuple[str, str]] = set()
    for row in rows[1:]:
        if len(row) <= max(lot_idx, code_idx):
            continue
        lot = clean_text(row[lot_idx])
        code = clean_text(row[code_idx])
        if lot and code:
            keys.add((lot, code))
    return keys


def write_csv(path: Path, records: List[LotRecord]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(OUTPUT_HEADERS)
        for record in records:
            writer.writerow(record.as_row())


def write_summary(path: Path, year: int, codes_count: int, records: List[LotRecord]) -> None:
    by_method: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for r in records:
        by_method[r.procurement_method] = by_method.get(r.procurement_method, 0) + 1
        by_status[r.status] = by_status.get(r.status, 0) + 1

    lines = [
        f"Год: {year}",
        f"Кодов ТРУ обработано: {codes_count}",
        f"Найдено лотов: {len(records)}",
        "",
        "Топ-20 способов закупки:",
    ]
    for method, cnt in sorted(by_method.items(), key=lambda x: x[1], reverse=True)[:20]:
        lines.append(f"- {method}: {cnt}")
    lines.append("")
    lines.append("Топ-20 статусов:")
    for status, cnt in sorted(by_status.items(), key=lambda x: x[1], reverse=True)[:20]:
        lines.append(f"- {status}: {cnt}")

    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сбор лотов за год по списку кодов ТРУ из Google Sheet."
    )
    parser.add_argument("--year", type=int, default=2025, help="Финансовый год для фильтра")
    parser.add_argument(
        "--per-page",
        type=int,
        default=2000,
        help="Размер страницы на goszakup (count_record)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.02,
        help="Пауза между страницами одного кода (сек.)",
    )
    parser.add_argument(
        "--status-id",
        type=int,
        default=360,
        help="ID статуса лота (по умолчанию 360 = Закупка состоялась)",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Минимальная сумма закупки (filter[amount_from])",
    )
    parser.add_argument(
        "--amount-to",
        default="",
        help="Максимальная сумма закупки (filter[amount_to])",
    )
    parser.add_argument(
        "--code-limit",
        type=int,
        default=0,
        help="Ограничить число кодов (0 = без ограничения)",
    )
    parser.add_argument(
        "--resume-from",
        default="",
        help="Начать с указанного кода ТРУ (включительно)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Исключить записи, уже присутствующие в целевой таблице (по ключу № лота + Код ТРУ)",
    )
    parser.add_argument(
        "--output",
        default="lots_2025_output.csv",
        help="Путь до итогового CSV файла",
    )
    parser.add_argument(
        "--summary",
        default="lots_2025_summary.txt",
        help="Путь до текстового summary-файла",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=50,
        help="Сохранять промежуточный CSV/summary каждые N обработанных кодов",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    session = make_session()
    tru_codes = read_tru_codes(session)

    if args.resume_from:
        start_idx = 0
        for idx, item in enumerate(tru_codes):
            if item.code == args.resume_from:
                start_idx = idx
                break
        tru_codes = tru_codes[start_idx:]

    if args.code_limit and args.code_limit > 0:
        tru_codes = tru_codes[: args.code_limit]

    existing_keys: Set[Tuple[str, str]] = set()
    if args.skip_existing:
        existing_keys = fetch_existing_rows(session)
        print(f"Уже в целевой таблице: {len(existing_keys)} ключей")

    all_records: List[LotRecord] = []
    processed_codes = 0
    out_path = Path(args.output)
    summary_path = Path(args.summary)
    total_codes = len(tru_codes)
    for i, tru in enumerate(tru_codes, start=1):
        try:
            records = fetch_for_code(
                session=session,
                tru=tru,
                year=args.year,
                per_page=args.per_page,
                delay=args.delay,
                status_id=args.status_id,
                amount_from=args.amount_from,
                amount_to=args.amount_to,
            )
            if args.skip_existing and records:
                before = len(records)
                records = [
                    r
                    for r in records
                    if (r.lot_number, r.tru_code) not in existing_keys
                ]
                after = len(records)
                if before != after:
                    print(
                        f"[{i}/{total_codes}] {tru.code}: найдено {before}, новых {after}"
                    )
                else:
                    print(f"[{i}/{total_codes}] {tru.code}: найдено {after}")
            else:
                print(f"[{i}/{total_codes}] {tru.code}: найдено {len(records)}")

            all_records.extend(records)
            processed_codes += 1

            if args.checkpoint_every > 0 and processed_codes % args.checkpoint_every == 0:
                temp_seen: Set[Tuple[str, str]] = set()
                temp_deduped: List[LotRecord] = []
                for row in all_records:
                    key = (row.lot_number, row.tru_code)
                    if key in temp_seen:
                        continue
                    temp_seen.add(key)
                    temp_deduped.append(row)
                write_csv(out_path, temp_deduped)
                write_summary(summary_path, args.year, processed_codes, temp_deduped)
                print(
                    f"Checkpoint: обработано {processed_codes}/{total_codes}, "
                    f"уникальных лотов {len(temp_deduped)}"
                )
        except requests.RequestException as exc:
            print(f"[{i}/{total_codes}] {tru.code}: ошибка запроса ({exc})")
            time.sleep(1.0)
            continue

    # remove duplicates by (lot_number, tru_code), preserving first occurrence
    deduped: List[LotRecord] = []
    seen: Set[Tuple[str, str]] = set()
    for row in all_records:
        key = (row.lot_number, row.tru_code)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    write_csv(out_path, deduped)
    write_summary(summary_path, args.year, total_codes, deduped)

    print(f"Готово. Кодов: {total_codes}, лотов (уникальных): {len(deduped)}")
    print(f"CSV: {out_path.resolve()}")
    print(f"Summary: {summary_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
