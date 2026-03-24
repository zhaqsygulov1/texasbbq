#!/usr/bin/env python3
import argparse
import csv
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlencode

import requests


SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
TARGET_SHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU"
SOURCE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}/gviz/tq?tqx=out:csv"
)
TARGET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{TARGET_SHEET_ID}/gviz/tq?tqx=out:csv"
)
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
FULL_ENSTRU_RE = re.compile(r"^\d{6}\.\d{3}\.\d{6}$")


HEADER = [
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


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_int_spaces(value: str) -> int | None:
    match = re.search(r"(\d[\d ]*)", value)
    if not match:
        return None
    return int(match.group(1).replace(" ", ""))


class LotsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_target_table = False
        self.in_tbody = False
        self.in_row = False
        self.in_cell = False
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: v for k, v in attrs}

        if tag == "table" and attrs_map.get("id") == "search-result":
            self.in_target_table = True
            return

        if not self.in_target_table:
            return

        if tag == "tbody":
            self.in_tbody = True
            return

        if not self.in_tbody:
            return

        if tag == "tr":
            self.in_row = True
            self._row = []
            return

        if not self.in_row:
            return

        if tag == "td":
            if self.in_cell:
                self._finalize_cell()
            self.in_cell = True
            self._cell_parts = []
            return

        if self.in_cell and tag == "br":
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_target_table:
            if self.in_cell:
                self._finalize_cell()
            self.in_target_table = False
            self.in_tbody = False
            self.in_row = False
            return

        if not self.in_target_table:
            return

        if tag == "tbody" and self.in_tbody:
            if self.in_cell:
                self._finalize_cell()
            self.in_tbody = False
            return

        if not self.in_tbody:
            return

        if tag == "td" and self.in_cell:
            self._finalize_cell()
            return

        if tag == "tr" and self.in_row:
            if self.in_cell:
                self._finalize_cell()
            if self._row:
                self.rows.append(self._row)
            self.in_row = False
            self._row = []

    def handle_data(self, data: str) -> None:
        if self.in_target_table and self.in_tbody and self.in_row and self.in_cell:
            self._cell_parts.append(data)

    def _finalize_cell(self) -> None:
        self._row.append(clean_text("".join(self._cell_parts)))
        self._cell_parts = []
        self.in_cell = False


@dataclass
class TruCode:
    code: str
    title: str


@dataclass
class FallbackSignature:
    total_records: int | None
    first_lot_numbers: list[str]


class IgnoredFilterResponseError(RuntimeError):
    pass


def http_get_with_retries(
    session: requests.Session, url: str, retries: int = 5, timeout: int = 90
) -> str:
    backoff = 2.0
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise RuntimeError(f"GET failed after {retries} attempts: {url}") from exc
            sleep_for = backoff * attempt
            eprint(f"[warn] {exc}; retry in {sleep_for:.1f}s: {url}")
            time.sleep(sleep_for)
    raise RuntimeError("unreachable")


def load_tru_codes(session: requests.Session, source_url: str) -> list[TruCode]:
    text = http_get_with_retries(session, source_url, retries=4, timeout=60)
    rows = csv.reader(text.splitlines())
    result: list[TruCode] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        if idx == 0:
            continue
        if not row:
            continue
        code = clean_text(row[0] if len(row) > 0 else "")
        title = clean_text(row[1] if len(row) > 1 else "")
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append(TruCode(code=code, title=title))
    return result


def parse_total_records(page_html: str) -> int | None:
    match = re.search(
        r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d ]+)\s*записей",
        page_html,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return parse_int_spaces(match.group(1))


def extract_lot_numbers(rows: list[list[str]], limit: int = 20) -> list[str]:
    lot_numbers: list[str] = []
    for row in rows:
        if not row:
            continue
        lot_no = clean_text(row[0])
        if not lot_no:
            continue
        lot_numbers.append(lot_no)
        if len(lot_numbers) >= limit:
            break
    return lot_numbers


def looks_like_fallback_response(
    rows: list[list[str]],
    total_records: int | None,
    signature: FallbackSignature,
) -> bool:
    if not rows or not signature.first_lot_numbers:
        return False
    if signature.total_records is not None and total_records != signature.total_records:
        return False

    current_lots = extract_lot_numbers(rows, limit=20)
    if not current_lots:
        return False

    sample = min(len(current_lots), len(signature.first_lot_numbers), 20)
    # 10 совпадающих первых лотов практически исключают случайное совпадение.
    if sample < 10:
        return False
    return current_lots[:sample] == signature.first_lot_numbers[:sample]


def build_fallback_signature(
    session: requests.Session,
    year: int,
    status_code: int,
    amount_from: int,
    per_page: int,
) -> FallbackSignature | None:
    params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": "INVALID_ENSTRU_CODE_FOR_SIGNATURE",
        "filter[status][0]": str(status_code),
        "filter[customer]": "",
        "filter[amount_from]": str(amount_from),
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": str(year),
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": str(per_page),
        "page": "1",
    }
    url = BASE_URL + "?" + urlencode(params, doseq=True)
    html = http_get_with_retries(session, url)

    parser = LotsTableParser()
    parser.feed(html)
    rows = parser.rows
    total_records = parse_total_records(html)
    first_lot_numbers = extract_lot_numbers(rows, limit=20)
    if not first_lot_numbers:
        return None
    return FallbackSignature(total_records=total_records, first_lot_numbers=first_lot_numbers)


def normalize_lot_row(cells: list[str], tru: TruCode) -> list[str] | None:
    if len(cells) < 7:
        return None

    lot_no = clean_text(cells[0])
    ann_name = clean_text(re.sub(r"\bЗаказчик:.*$", "", cells[1], flags=re.IGNORECASE))
    lot_name = clean_text(re.sub(r"\bИстория\b.*$", "", cells[2], flags=re.IGNORECASE))
    qty = clean_text(cells[3])
    amount = clean_text(cells[4])
    method = clean_text(cells[5])
    status = clean_text(cells[6])

    return [
        lot_no,
        tru.code,
        tru.title,
        ann_name,
        lot_name,
        qty,
        amount,
        method,
        status,
    ]


def fetch_lots_for_code(
    session: requests.Session,
    tru: TruCode,
    year: int,
    status_code: int,
    amount_from: int,
    per_page: int,
    pause_seconds: float,
    fallback_signature: FallbackSignature | None = None,
) -> Iterable[list[str]]:
    base_params = {
        "filter[name]": "",
        "filter[number]": "",
        "filter[number_anno]": "",
        "filter[enstru]": tru.code,
        "filter[status][0]": str(status_code),
        "filter[customer]": "",
        "filter[amount_from]": str(amount_from),
        "filter[amount_to]": "",
        "filter[trade_type]": "",
        "filter[month]": "",
        "filter[plan_number]": "",
        "filter[end_date_from]": "",
        "filter[end_date_to]": "",
        "filter[start_date_to]": "",
        "filter[year]": str(year),
        "filter[itogi_date_from]": "",
        "filter[itogi_date_to]": "",
        "filter[start_date_from]": "",
        "filter[more]": "",
        "smb": "",
        "count_record": str(per_page),
    }

    page = 1
    total_pages = None

    while True:
        params = dict(base_params)
        params["page"] = str(page)
        url = BASE_URL + "?" + urlencode(params, doseq=True)
        html = http_get_with_retries(session, url)

        parser = LotsTableParser()
        parser.feed(html)
        rows = parser.rows

        if page == 1:
            total_records = parse_total_records(html)
            if total_records is not None and per_page > 0:
                total_pages = max(1, math.ceil(total_records / per_page))
            if (
                fallback_signature is not None
                and looks_like_fallback_response(rows, total_records, fallback_signature)
            ):
                raise IgnoredFilterResponseError(
                    f"ENSTRU filter ignored for code={tru.code}; fallback signature detected"
                )

        if not rows:
            break

        for raw_row in rows:
            normalized = normalize_lot_row(raw_row, tru)
            if normalized:
                yield normalized

        if total_pages is not None and page >= total_pages:
            break

        if total_pages is None and len(rows) < per_page:
            break

        page += 1
        if pause_seconds > 0:
            time.sleep(pause_seconds)


def write_output(
    out_path: str,
    rows: Iterable[list[str]],
    append: bool = False,
) -> int:
    mode = "a" if append else "w"
    written = 0
    with open(out_path, mode, encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if not append:
            writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row)
            written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Сбор лотов goszakup за 2025 по кодам ТРУ из Google Sheets "
            "и сохранение в CSV с целевой структурой."
        )
    )
    parser.add_argument(
        "--source-url",
        default=SOURCE_CSV_URL,
        help="URL CSV/gviz таблицы с кодами ТРУ.",
    )
    parser.add_argument(
        "--output",
        default="/workspace/output/lots_2025_by_tru.csv",
        help="Путь выходного CSV.",
    )
    parser.add_argument("--year", type=int, default=2025, help="Финансовый год.")
    parser.add_argument(
        "--status-code",
        type=int,
        default=360,
        help="Код статуса в фильтре goszakup (360 = Закупка состоялась).",
    )
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15_000_000,
        help="Минимальная сумма закупки в фильтре.",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=50,
        help="Количество строк на страницу (count_record).",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Ограничить количество кодов для smoke test (0 = без ограничения).",
    )
    parser.add_argument(
        "--allow-nonfull-codes",
        action="store_true",
        help="Разрешить коды, не соответствующие формату 000000.000.000000.",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.3,
        help="Пауза между страницами одного кода.",
    )
    parser.add_argument(
        "--skipped-codes-output",
        default="/workspace/output/skipped_codes.csv",
        help="CSV для списка пропущенных кодов с причинами.",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )

    eprint(f"[info] source: {args.source_url}")
    loaded_codes = load_tru_codes(session, args.source_url)
    skipped_codes: list[tuple[str, str, str]] = []

    if args.allow_nonfull_codes:
        tru_codes = loaded_codes
    else:
        tru_codes = []
        for item in loaded_codes:
            if FULL_ENSTRU_RE.fullmatch(item.code):
                tru_codes.append(item)
            else:
                skipped_codes.append((item.code, item.title, "non_full_enstru_code"))
        eprint(
            f"[info] strict ENSTRU enabled: kept={len(tru_codes)} "
            f"skipped_non_full={len(skipped_codes)}"
        )

    if args.limit_codes > 0:
        tru_codes = tru_codes[: args.limit_codes]
    eprint(f"[info] loaded codes: {len(tru_codes)}")

    out_dir = re.sub(r"[^/]+$", "", args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    skipped_dir = re.sub(r"[^/]+$", "", args.skipped_codes_output)
    if skipped_dir:
        os.makedirs(skipped_dir, exist_ok=True)

    fallback_signature = build_fallback_signature(
        session=session,
        year=args.year,
        status_code=args.status_code,
        amount_from=args.amount_from,
        per_page=args.per_page,
    )
    if fallback_signature is None:
        eprint("[warn] fallback signature not detected; proceeding without fallback checks")
    else:
        eprint(
            f"[info] fallback signature: total={fallback_signature.total_records} "
            f"first_lots={len(fallback_signature.first_lot_numbers)}"
        )

    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)

        total_written = 0
        start_ts = time.time()
        for idx, tru in enumerate(tru_codes, start=1):
            code_rows = 0
            try:
                for row in fetch_lots_for_code(
                    session=session,
                    tru=tru,
                    year=args.year,
                    status_code=args.status_code,
                    amount_from=args.amount_from,
                    per_page=args.per_page,
                    pause_seconds=args.pause_seconds,
                    fallback_signature=fallback_signature,
                ):
                    writer.writerow(row)
                    code_rows += 1
                    total_written += 1
            except IgnoredFilterResponseError as exc:
                skipped_codes.append((tru.code, tru.title, "ignored_filter_fallback_signature"))
                eprint(f"[warn] {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                eprint(f"[error] code={tru.code} failed: {exc}")
                skipped_codes.append((tru.code, tru.title, f"error: {exc}"))
                continue

            elapsed = time.time() - start_ts
            eprint(
                f"[progress] {idx}/{len(tru_codes)} code={tru.code} "
                f"rows={code_rows} total={total_written} elapsed={elapsed:.1f}s"
            )

    eprint(f"[done] output: {args.output}; rows={total_written}")
    with open(args.skipped_codes_output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Код ТРУ", "Название", "Причина пропуска"])
        writer.writerows(skipped_codes)
    eprint(f"[done] skipped codes: {args.skipped_codes_output}; rows={len(skipped_codes)}")
    eprint(f"[hint] target sheet read URL: {TARGET_CSV_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
