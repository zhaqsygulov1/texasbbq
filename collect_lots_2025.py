#!/usr/bin/env python3
import argparse
import csv
import io
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


SOURCE_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv&gid=0"
)
BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
STATUS_COMPLETED = "360"


@dataclass
class CodeRow:
    code: str
    name: str


@dataclass
class LotRow:
    lot_number: str
    code: str
    product_name: str
    announcement_name: str
    lot_name_description: str
    qty: str
    amount_kzt: str
    procurement_method: str
    status: str


@dataclass
class CollectResult:
    rows: List[LotRow]
    total: int
    treated_as_unfiltered_fallback: bool = False


def norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def load_source_codes(session: requests.Session) -> List[CodeRow]:
    resp = session.get(SOURCE_SHEET_CSV, timeout=60)
    resp.raise_for_status()
    decoded = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded))
    rows: List[CodeRow] = []
    for row in reader:
        code = norm_space(row.get("Код ТРУ", ""))
        name = norm_space(row.get("Название", ""))
        if not code:
            continue
        rows.append(CodeRow(code=code, name=name))
    return rows


def parse_total_records(html: str) -> Optional[int]:
    match = re.search(r"Показано c\s*\d+\s*по\s*\d+\s*из\s*([\d\s]+)\s*записей", html)
    if not match:
        if "из 0 записей" in html:
            return 0
        return None
    return int(re.sub(r"\D", "", match.group(1)) or "0")


def get_lots_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [norm_space(th.get_text(" ", strip=True)) for th in table.select("th")]
        if headers and "№ лота" in headers:
            return table
    return None


def parse_lot_numbers_from_html(html: str, limit: int = 20) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    table = get_lots_table(soup)
    if table is None:
        return []
    numbers: List[str] = []
    for tr in table.select("tr")[1:]:
        tds = tr.select("td")
        if not tds:
            continue
        lot_number = norm_space(tds[0].get_text(" ", strip=True))
        if not lot_number:
            continue
        numbers.append(lot_number)
        if len(numbers) >= limit:
            break
    return numbers


def parse_lots_from_html(html: str, code: str, product_name: str) -> List[LotRow]:
    soup = BeautifulSoup(html, "lxml")
    table = get_lots_table(soup)
    if table is None:
        return []

    out: List[LotRow] = []
    trs = table.select("tr")
    for tr in trs[1:]:
        tds = tr.select("td")
        if len(tds) < 7:
            continue

        lot_number = norm_space(tds[0].get_text(" ", strip=True))

        ann_anchor = tds[1].select_one("a")
        announcement_name = norm_space(
            ann_anchor.get_text(" ", strip=True) if ann_anchor else tds[1].get_text(" ", strip=True)
        )

        lot_anchor = tds[2].select_one("a")
        lot_name_description = norm_space(
            lot_anchor.get_text(" ", strip=True) if lot_anchor else tds[2].get_text(" ", strip=True)
        )

        qty = norm_space(tds[3].get_text(" ", strip=True))
        amount_kzt = norm_space(tds[4].get_text(" ", strip=True))
        procurement_method = norm_space(tds[5].get_text(" ", strip=True))
        status = norm_space(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        out.append(
            LotRow(
                lot_number=lot_number,
                code=code,
                product_name=product_name,
                announcement_name=announcement_name,
                lot_name_description=lot_name_description,
                qty=qty,
                amount_kzt=amount_kzt,
                procurement_method=procurement_method,
                status=status,
            )
        )
    return out


def request_with_retry(
    session: requests.Session, params: List[Tuple[str, str]], retries: int = 5
) -> str:
    wait = 1.0
    last_err: Optional[Exception] = None
    for _ in range(retries):
        try:
            resp = session.get(BASE_URL, params=params, timeout=60)
            if resp.status_code >= 500:
                raise RuntimeError(f"Server error {resp.status_code}")
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(wait)
            wait = min(wait * 2.0, 16.0)
    raise RuntimeError(f"Request failed after retries: {last_err}")


def build_params(code: str, year: str, amount_from: str, page: int = 1) -> List[Tuple[str, str]]:
    params: List[Tuple[str, str]] = [
        ("filter[enstru]", code),
        ("filter[year]", year),
        ("filter[status][]", STATUS_COMPLETED),
        ("filter[amount_from]", amount_from),
        ("count_record", "2000"),
        ("page", str(page)),
    ]
    return params


def collect_code(
    session: requests.Session,
    code: str,
    product_name: str,
    year: str,
    amount_from: str,
    baseline_signature: Optional[Tuple[str, ...]] = None,
    signature_size: int = 20,
) -> CollectResult:
    first_html = request_with_retry(session, build_params(code, year, amount_from, page=1))
    total = parse_total_records(first_html)
    if total is None:
        return CollectResult(rows=[], total=0)

    # goszakup sometimes ignores invalid ENSTRU values and returns a generic
    # top-10000 feed. Detect this by comparing the first lot numbers with a
    # known fallback signature and discard such false positives.
    if baseline_signature and total == 10000:
        current_signature = tuple(parse_lot_numbers_from_html(first_html, limit=signature_size))
        if current_signature and current_signature == baseline_signature:
            return CollectResult(rows=[], total=0, treated_as_unfiltered_fallback=True)

    rows = parse_lots_from_html(first_html, code, product_name)

    if total <= 2000:
        return CollectResult(rows=rows, total=total)

    pages = int(math.ceil(total / 2000))
    for page in range(2, pages + 1):
        html = request_with_retry(session, build_params(code, year, amount_from, page=page))
        rows.extend(parse_lots_from_html(html, code, product_name))
    return CollectResult(rows=rows, total=total)


def write_csv(rows: Iterable[LotRow], output_path: str) -> None:
    header = [
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
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in rows:
            writer.writerow(
                [
                    r.lot_number,
                    r.code,
                    r.product_name,
                    r.announcement_name,
                    r.lot_name_description,
                    r.qty,
                    r.amount_kzt,
                    r.procurement_method,
                    r.status,
                ]
            )


def dedupe_preserve_order(code_rows: List[CodeRow]) -> List[CodeRow]:
    seen = set()
    out: List[CodeRow] = []
    for row in code_rows:
        if row.code in seen:
            continue
        seen.add(row.code)
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect goszakup lots by TRU code.")
    parser.add_argument("--year", default="2025")
    parser.add_argument("--amount-from", default="15000000")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument(
        "--output",
        default="lots_2025_by_tru.csv",
        help="Output CSV path",
    )
    parser.add_argument(
        "--signature-size",
        type=int,
        default=20,
        help="How many first lot numbers to compare for fallback detection",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en;q=0.9",
        }
    )

    source_rows = load_source_codes(session)
    unique_codes = dedupe_preserve_order(source_rows)
    print(f"Loaded {len(source_rows)} rows from source, unique codes: {len(unique_codes)}")

    baseline_html = request_with_retry(
        session,
        build_params("__INVALID_ENSTRU_FILTER__", args.year, args.amount_from, page=1),
    )
    baseline_signature = tuple(
        parse_lot_numbers_from_html(baseline_html, limit=args.signature_size)
    )
    print(
        "Fallback signature prepared with "
        f"{len(baseline_signature)} lot numbers for invalid ENSTRU detection"
    )

    lock = threading.Lock()
    all_rows: List[LotRow] = []
    processed = 0
    nonzero_codes = 0
    fallback_codes = 0
    failed_codes: List[str] = []

    def worker(code_row: CodeRow) -> Tuple[str, CollectResult]:
        local_session = requests.Session()
        local_session.headers.update(session.headers)
        result = collect_code(
            local_session, code_row.code, code_row.name, year=args.year, amount_from=args.amount_from
            , baseline_signature=baseline_signature
            , signature_size=args.signature_size
        )
        return code_row.code, result

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(worker, c) for c in unique_codes]
        for fut in as_completed(futures):
            try:
                code, result = fut.result()
                with lock:
                    processed += 1
                    if result.total > 0:
                        nonzero_codes += 1
                    if result.treated_as_unfiltered_fallback:
                        fallback_codes += 1
                    all_rows.extend(result.rows)
                    if processed % 25 == 0 or processed == len(unique_codes):
                        print(
                            f"Processed {processed}/{len(unique_codes)} codes; "
                            f"rows={len(all_rows)}; nonzero_codes={nonzero_codes}; "
                            f"fallback_codes={fallback_codes}"
                        )
            except Exception as exc:  # noqa: BLE001
                with lock:
                    processed += 1
                    failed_codes.append(str(exc))
                    if processed % 25 == 0 or processed == len(unique_codes):
                        print(
                            f"Processed {processed}/{len(unique_codes)} codes; "
                            f"rows={len(all_rows)}; failures={len(failed_codes)}"
                        )

    # Deduplicate by lot number + code to avoid accidental repeats from source duplicates.
    deduped: List[LotRow] = []
    seen_keys = set()
    for row in all_rows:
        key = (row.lot_number, row.code)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(row)

    write_csv(deduped, args.output)
    print(f"Wrote {len(deduped)} rows to {args.output}")
    print(f"Codes treated as invalid/unfiltered fallback: {fallback_codes}")
    print(f"Failed code requests (count): {len(failed_codes)}")


if __name__ == "__main__":
    main()
