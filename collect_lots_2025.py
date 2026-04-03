#!/usr/bin/env python3
import argparse
import csv
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Tuple

import requests


BASE_URL = "https://goszakup.gov.kz/ru/search/lots"
OUT_HEADER = [
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

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class CodeTask:
    code: str
    product_name: str


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value, flags=re.S)
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\bИстория\b", "", value).strip()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_first_strong(cell_html: str) -> str:
    m = re.search(r"<strong>(.*?)</strong>", cell_html, flags=re.S | re.I)
    if not m:
        return clean_text(cell_html)
    return clean_text(m.group(1))


class SearchLotsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_search_table = False
        self.table_depth = 0
        self.in_tbody = False
        self.in_tr = False
        self.current_row: List[str] = []
        self.current_cell_parts: List[str] | None = None
        self.rows: List[List[str]] = []

    @staticmethod
    def _attrs_to_dict(attrs: List[Tuple[str, str | None]]) -> Dict[str, str]:
        return {k: (v or "") for k, v in attrs}

    def _finalize_cell(self) -> None:
        if self.current_cell_parts is None:
            return
        raw = "".join(self.current_cell_parts)
        # Keep line breaks to separate title from secondary details.
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n+", "\n", raw)
        cell = raw.strip()
        self.current_row.append(cell)
        self.current_cell_parts = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        if tag == "table":
            attrs_dict = self._attrs_to_dict(attrs)
            if self.in_search_table:
                self.table_depth += 1
            elif attrs_dict.get("id") == "search-result":
                self.in_search_table = True
                self.table_depth = 1
            return

        if not self.in_search_table:
            return

        if tag == "tbody":
            self.in_tbody = True
            return

        if not self.in_tbody:
            return

        if tag == "tr":
            self.in_tr = True
            self.current_row = []
            self.current_cell_parts = None
            return

        if not self.in_tr:
            return

        if tag == "td":
            # Gracefully handle malformed HTML where </td> is omitted.
            if self.current_cell_parts is not None:
                self._finalize_cell()
            self.current_cell_parts = []
        elif tag == "small" and self.current_cell_parts is not None:
            # Secondary details such as "Заказчик:" should not pollute the title.
            self.current_cell_parts.append("\n")
        elif tag == "br" and self.current_cell_parts is not None:
            self.current_cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.in_search_table and tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_search_table = False
                self.table_depth = 0
                self.in_tbody = False
                self.in_tr = False
                self.current_cell_parts = None
            return

        if not self.in_search_table:
            return

        if tag == "tbody":
            self.in_tbody = False
            return

        if not self.in_tbody:
            return

        if tag == "td":
            self._finalize_cell()
            return

        if tag == "tr" and self.in_tr:
            if self.current_cell_parts is not None:
                self._finalize_cell()
            if self.current_row:
                self.rows.append(self.current_row[:])
            self.in_tr = False
            self.current_row = []
            self.current_cell_parts = None

    def handle_data(self, data: str) -> None:
        if self.in_search_table and self.in_tbody and self.in_tr and self.current_cell_parts is not None:
            self.current_cell_parts.append(data)


def parse_total_records(html: str) -> int:
    m = re.search(r"Показано c\s+\d+\s+по\s+\d+\s+из\s+([\d ]+)\s+записей", html)
    if not m:
        return 0
    return int(m.group(1).replace(" ", ""))


def parse_rows_from_html(html: str, code: str, product_name: str) -> List[Dict[str, str]]:
    parser = SearchLotsTableParser()
    parser.feed(html)
    parser.close()

    output_rows: List[Dict[str, str]] = []

    for cells in parser.rows:
        if len(cells) < 7:
            continue

        lot_no = normalize_spaces(cells[0].split("\n")[0])
        announce_name = normalize_spaces(cells[1].split("\n")[0])
        lot_name_desc = normalize_spaces(cells[2].split("\n")[0])
        quantity = clean_text(cells[3])
        amount = clean_text(cells[4])
        method = clean_text(cells[5])
        status = clean_text(cells[6])

        if not lot_no:
            continue

        output_rows.append(
            {
                "№ лота": lot_no,
                "Код ТРУ": code,
                "Наименование товара": product_name,
                "Наименование объявления": announce_name,
                "Наименование и описание лота": lot_name_desc,
                "Кол-во": quantity,
                "Сумма, тг.": amount,
                "Способ закупки": method,
                "Статус": status,
            }
        )

    return output_rows


def request_with_retries(session: requests.Session, params: Dict[str, str], retries: int = 5) -> str:
    wait_base = 1.2
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(BASE_URL, params=params, timeout=35)
            resp.raise_for_status()
            if "№ лота" not in resp.text and "search-result" not in resp.text:
                raise RuntimeError("Unexpected response layout")
            return resp.text
        except Exception:
            if attempt == retries:
                raise
            sleep_for = (wait_base * (2 ** (attempt - 1))) + random.uniform(0.2, 0.9)
            time.sleep(sleep_for)
    raise RuntimeError("unreachable")


def collect_for_code(task: CodeTask, year: int, status: int, amount_from: int, count_record: int) -> Tuple[str, List[Dict[str, str]]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    base_params = {
        "filter[year]": str(year),
        "filter[enstru]": task.code,
        "filter[status][]": str(status),
        "filter[amount_from]": str(amount_from),
        "count_record": str(count_record),
        "smb": "",
    }

    first_html = request_with_retries(session, {**base_params, "page": "1"})
    rows = parse_rows_from_html(first_html, task.code, task.product_name)

    total_records = parse_total_records(first_html)
    if total_records <= count_record:
        return task.code, rows

    total_pages = math.ceil(total_records / count_record)
    for page in range(2, total_pages + 1):
        html = request_with_retries(session, {**base_params, "page": str(page)})
        rows.extend(parse_rows_from_html(html, task.code, task.product_name))
        # Mild jitter to reduce anti-bot throttling.
        time.sleep(random.uniform(0.15, 0.45))

    return task.code, rows


def load_source_codes(source_csv: Path) -> List[CodeTask]:
    tasks: List[CodeTask] = []
    with source_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("Код ТРУ") or "").strip()
            name = (row.get("Название") or "").strip()
            if not code:
                continue
            tasks.append(CodeTask(code=code, product_name=name))
    return tasks


def load_done_codes(progress_file: Path) -> set:
    if not progress_file.exists():
        return set()
    done = set()
    with progress_file.open("r", encoding="utf-8") as f:
        for line in f:
            code = line.strip()
            if code:
                done.add(code)
    return done


def append_done_code(progress_file: Path, code: str, lock: threading.Lock) -> None:
    with lock:
        with progress_file.open("a", encoding="utf-8") as f:
            f.write(code + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect goszakup 2025 lots by TRU codes from source CSV."
    )
    parser.add_argument(
        "--source-csv",
        default="source_tru_codes.csv",
        help="Input CSV with columns: Код ТРУ, Название",
    )
    parser.add_argument(
        "--output-csv",
        default="lots_2025_by_tru.csv",
        help="Output CSV in required report format",
    )
    parser.add_argument(
        "--progress-file",
        default="lots_2025_by_tru.progress.txt",
        help="File that stores completed TRU codes",
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status", type=int, default=360)
    parser.add_argument("--amount-from", type=int, default=15000000, dest="amount_from")
    parser.add_argument("--count-record", type=int, default=500, dest="count_record")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="Process only first N pending codes")
    args = parser.parse_args()

    source_csv = Path(args.source_csv)
    output_csv = Path(args.output_csv)
    progress_file = Path(args.progress_file)

    tasks = load_source_codes(source_csv)
    done_codes = load_done_codes(progress_file)
    pending_tasks = [t for t in tasks if t.code not in done_codes]
    if args.limit > 0:
        pending_tasks = pending_tasks[: args.limit]

    output_exists = output_csv.exists() and output_csv.stat().st_size > 0
    out_lock = threading.Lock()
    progress_lock = threading.Lock()

    with output_csv.open("a", encoding="utf-8", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUT_HEADER)
        if not output_exists:
            writer.writeheader()

        total_pending = len(pending_tasks)
        print(f"Source codes: {len(tasks)} | already done: {len(done_codes)} | pending: {total_pending}")
        if total_pending == 0:
            print("Nothing to do.")
            return

        success_count = 0
        failed: List[Tuple[str, str]] = []
        rows_written = 0
        started = time.time()

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    collect_for_code,
                    task,
                    args.year,
                    args.status,
                    args.amount_from,
                    args.count_record,
                ): task
                for task in pending_tasks
            }

            for idx, future in enumerate(as_completed(future_map), start=1):
                task = future_map[future]
                try:
                    code, rows = future.result()
                    with out_lock:
                        for row in rows:
                            writer.writerow(row)
                        out_f.flush()
                    rows_written += len(rows)
                    success_count += 1
                    append_done_code(progress_file, code, progress_lock)
                    elapsed = time.time() - started
                    print(
                        f"[{idx}/{total_pending}] OK {code}: {len(rows)} rows | "
                        f"rows_total={rows_written} | elapsed={elapsed:.1f}s"
                    )
                except Exception as exc:
                    failed.append((task.code, str(exc)))
                    elapsed = time.time() - started
                    print(f"[{idx}/{total_pending}] FAIL {task.code}: {exc} | elapsed={elapsed:.1f}s")

        print(
            "Done. "
            f"success={success_count}, failed={len(failed)}, rows_written={rows_written}, "
            f"output={output_csv}"
        )
        if failed:
            failed_csv = output_csv.with_suffix(".failed.csv")
            with failed_csv.open("w", encoding="utf-8", newline="") as f:
                fw = csv.writer(f)
                fw.writerow(["Код ТРУ", "Ошибка"])
                fw.writerows(failed)
            print(f"Failed list saved to: {failed_csv}")


if __name__ == "__main__":
    main()
