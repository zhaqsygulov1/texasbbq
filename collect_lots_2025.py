#!/usr/bin/env python3
import argparse
import csv
import html
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
META_RE = re.compile(r"Показано c\s*(\d+)\s*по\s*(\d+)\s*из\s*(\d+)\s*записей", re.IGNORECASE)
TABLE_RE = re.compile(
    r'<table id="search-result".*?<tbody>(.*?)</tbody>',
    re.IGNORECASE | re.DOTALL,
)
ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
TD_SPLIT_RE = re.compile(r"<td[^>]*>", re.IGNORECASE | re.DOTALL)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.IGNORECASE | re.DOTALL)
A_STRONG_RE = re.compile(r"<a[^>]*>\s*<strong>(.*?)</strong>\s*</a>", re.IGNORECASE | re.DOTALL)


@dataclass
class LotRow:
    lot_no: str
    announcement: str
    lot_name_desc: str
    quantity: str
    amount_tenge: str
    method: str
    status: str


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def clean_html_text(value: str) -> str:
    value = BR_RE.sub(" ", value)
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_row_cells(row_html: str) -> Optional[LotRow]:
    # The page HTML often has invalid <td> nesting (missing </td> in first cells).
    # Splitting by "<td...>" is more robust here than strict XML parsing.
    cells = TD_SPLIT_RE.split(row_html)
    cells = [c for c in cells[1:]]  # first element is content before first <td>
    if len(cells) < 7:
        return None

    lot_no_match = STRONG_RE.search(cells[0])
    lot_no = clean_html_text(lot_no_match.group(1)) if lot_no_match else clean_html_text(cells[0])

    ann_match = A_STRONG_RE.search(cells[1])
    announcement = clean_html_text(ann_match.group(1)) if ann_match else clean_html_text(cells[1])

    lot_match = A_STRONG_RE.search(cells[2])
    lot_name_desc = clean_html_text(lot_match.group(1)) if lot_match else clean_html_text(cells[2])

    quantity = clean_html_text(cells[3])
    amount_match = STRONG_RE.search(cells[4])
    amount_tenge = clean_html_text(amount_match.group(1)) if amount_match else clean_html_text(cells[4])
    method = clean_html_text(cells[5])
    status = clean_html_text(cells[6])

    if not lot_no:
        return None

    return LotRow(
        lot_no=lot_no,
        announcement=announcement,
        lot_name_desc=lot_name_desc,
        quantity=quantity,
        amount_tenge=amount_tenge,
        method=method,
        status=status,
    )


def parse_lot_rows(page_html: str) -> List[LotRow]:
    table_match = TABLE_RE.search(page_html)
    if not table_match:
        return []
    tbody = table_match.group(1)
    rows: List[LotRow] = []
    for row_html in ROW_RE.findall(tbody):
        row = parse_row_cells(row_html)
        if row is not None:
            rows.append(row)
    return rows


def parse_meta(page_html: str) -> Optional[Tuple[int, int, int]]:
    match = META_RE.search(page_html)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def fetch_page(
    session: requests.Session,
    tru_code: str,
    year: int,
    page: int,
    status: Optional[str],
    amount_from: Optional[str],
    count_record: int,
) -> str:
    params: Dict[str, str] = {
        "filter[enstru]": tru_code,
        "filter[year]": str(year),
        "count_record": str(count_record),
    }
    if page > 1:
        params["page"] = str(page)
    if status:
        params["filter[status][0]"] = status
    if amount_from:
        params["filter[amount_from]"] = amount_from

    last_error: Optional[Exception] = None
    for attempt in range(6):
        try:
            response = session.get(SEARCH_URL, params=params, timeout=(20, 120))
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 5:
                break
            sleep_for = (2 ** attempt) + random.random()
            time.sleep(sleep_for)

    raise RuntimeError(f"Failed to fetch code={tru_code} page={page}: {last_error}")


def iter_tru_rows(path: Path) -> Iterable[Tuple[str, str]]:
    with path.open(newline="", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            code = (row.get("Код ТРУ") or "").strip()
            name = (row.get("Название") or "").strip()
            if code:
                yield code, name


def run(args: argparse.Namespace) -> int:
    session = build_session()
    src_path = Path(args.source_csv)
    out_path = Path(args.output_csv)

    out_path.parent.mkdir(parents=True, exist_ok=True)

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

    total_codes = 0
    total_written = 0
    total_pages = 0
    failed_codes: List[str] = []
    seen: set[Tuple[str, str]] = set()

    rows_iter = list(iter_tru_rows(src_path))
    if args.max_codes is not None:
        rows_iter = rows_iter[: args.max_codes]

    with out_path.open("w", newline="", encoding="utf-8-sig") as outfile:
        writer = csv.writer(outfile)
        writer.writerow(header)

        for idx, (tru_code, product_name) in enumerate(rows_iter, start=1):
            total_codes += 1
            page = 1
            code_written = 0
            code_failed = False

            while page <= args.max_pages_per_code:
                try:
                    page_html = fetch_page(
                        session=session,
                        tru_code=tru_code,
                        year=args.year,
                        page=page,
                        status=args.status,
                        amount_from=args.amount_from,
                        count_record=args.count_record,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] code={tru_code} page={page} failed: {exc}", file=sys.stderr)
                    code_failed = True
                    break

                rows = parse_lot_rows(page_html)
                total_pages += 1
                if not rows:
                    break

                for row in rows:
                    dedupe_key = (row.lot_no, tru_code)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    writer.writerow(
                        [
                            row.lot_no,
                            tru_code,
                            product_name,
                            row.announcement,
                            row.lot_name_desc,
                            row.quantity,
                            row.amount_tenge,
                            row.method,
                            row.status,
                        ]
                    )
                    total_written += 1
                    code_written += 1

                meta = parse_meta(page_html)
                if (
                    args.stop_by_meta
                    and meta is not None
                    and meta[2] > 0
                    and meta[1] >= meta[2]
                ):
                    break

                page += 1
                time.sleep(random.uniform(args.sleep_min, args.sleep_max))

            if code_failed:
                failed_codes.append(tru_code)

            if idx % args.progress_every == 0:
                print(
                    f"[INFO] processed={idx}/{len(rows_iter)} "
                    f"code={tru_code} pages={page} code_rows={code_written} total_rows={total_written}",
                    file=sys.stderr,
                )

    print(
        f"[DONE] codes={total_codes} pages={total_pages} rows={total_written} "
        f"failed_codes={len(failed_codes)} output={out_path}",
        file=sys.stderr,
    )

    if failed_codes:
        fail_path = out_path.with_suffix(".failed_codes.txt")
        fail_path.write_text("\n".join(failed_codes), encoding="utf-8")
        print(f"[DONE] failed code list saved to: {fail_path}", file=sys.stderr)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect 2025 lots from goszakup by TRU codes and save CSV."
    )
    parser.add_argument(
        "--source-csv",
        default="/workspace/tru_codes.csv",
        help="Input CSV with columns: Код ТРУ, Название",
    )
    parser.add_argument(
        "--output-csv",
        default="/workspace/lots_2025_by_tru.csv",
        help="Output CSV path",
    )
    parser.add_argument("--year", type=int, default=2025, help="Year filter")
    parser.add_argument(
        "--status",
        default="360",
        help="Status code filter (default: 360, Закупка состоялась). Empty string disables filter.",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Minimum amount filter. Empty string disables filter.",
    )
    parser.add_argument(
        "--count-record",
        type=int,
        default=2000,
        help="Rows requested per page (max supported by portal: 2000).",
    )
    parser.add_argument(
        "--max-pages-per-code",
        type=int,
        default=200,
        help="Safety cap for pages per single TRU code.",
    )
    parser.add_argument(
        "--max-codes",
        type=int,
        default=None,
        help="Limit amount of TRU codes for dry runs.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress every N codes.",
    )
    parser.add_argument(
        "--sleep-min",
        type=float,
        default=0.05,
        help="Min delay between pages for one code.",
    )
    parser.add_argument(
        "--sleep-max",
        type=float,
        default=0.20,
        help="Max delay between pages for one code.",
    )
    parser.add_argument(
        "--stop-by-meta",
        action="store_true",
        help="Stop code parsing when page meta says current_end >= total. "
        "Disabled by default because portal often truncates total to 10000.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.status == "":
        args.status = None
    if args.amount_from == "":
        args.amount_from = None

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
