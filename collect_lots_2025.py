#!/usr/bin/env python3
"""Collect lots from goszakup for TRU codes from a Google Sheet export.

Default behavior follows the sample URL provided by the requester:
  - year: 2025
  - status: 360 ("Закупка состоялась")
  - amount_from: 15000000
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
DEFAULT_CODES_CSV = (
    "https://docs.google.com/spreadsheets/d/"
    "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
PER_PAGE = 2000
SHOW_RE = re.compile(r"Показано c\s*(\d+)\s*по\s*(\d+)\s*из\s*(\d+)\s*записей")
CSV_HEADER = [
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


@dataclass
class LotRow:
    lot_no: str
    tru_code: str
    product_name: str
    announcement_name: str
    lot_name_desc: str
    qty: str
    amount_tenge: str
    procurement_method: str
    status: str

    def as_list(self) -> list[str]:
        return [
            self.lot_no,
            self.tru_code,
            self.product_name,
            self.announcement_name,
            self.lot_name_desc,
            self.qty,
            self.amount_tenge,
            self.procurement_method,
            self.status,
        ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect 2025 lots by TRU codes from goszakup."
    )
    parser.add_argument(
        "--codes-csv-url",
        default=DEFAULT_CODES_CSV,
        help="Google Sheets CSV export URL with two columns: code, name",
    )
    parser.add_argument(
        "--codes-csv-path",
        default="tru_codes.csv",
        help="Local CSV path to use/save TRU codes",
    )
    parser.add_argument(
        "--year",
        default="2025",
        help="Financial year filter",
    )
    parser.add_argument(
        "--status",
        default="360",
        help="Status filter code (360 = Закупка состоялась). Empty disables.",
    )
    parser.add_argument(
        "--amount-from",
        default="15000000",
        help="Minimal amount filter. Empty disables.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel workers",
    )
    parser.add_argument(
        "--limit-codes",
        type=int,
        default=0,
        help="Process only first N codes (0 = all)",
    )
    parser.add_argument(
        "--raw-csv",
        default="lots_2025_by_tru_raw.csv",
        help="Append-only raw CSV with possible duplicates",
    )
    parser.add_argument(
        "--output-csv",
        default="lots_2025_by_tru.csv",
        help="Final deduplicated output CSV path",
    )
    parser.add_argument(
        "--stats-json",
        default="lots_2025_by_tru_stats.json",
        help="Stats JSON path",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=45,
        help="HTTP timeout seconds",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Request retries per page",
    )
    parser.add_argument(
        "--resume-json",
        default="lots_2025_progress.json",
        help="Checkpoint file with processed codes",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="Save progress every N processed codes",
    )
    return parser.parse_args()


def download_codes_csv(url: str, target_path: Path, timeout: int = 60) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    target_path.write_bytes(response.content)


def read_tru_codes(csv_path: Path) -> list[TruCode]:
    unique: dict[str, TruCode] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if not row:
                continue
            code = row[0].strip()
            if not code:
                continue
            name = row[1].strip() if len(row) > 1 else ""
            if code not in unique:
                unique[code] = TruCode(code=code, name=name)
    return list(unique.values())


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_cell_text(td) -> str:
    # Prefer first link text when available to avoid mixing in helper text.
    link = td.select_one("a")
    if link:
        return clean_text(link.get_text(" ", strip=True))
    return clean_text(td.get_text(" ", strip=True))


def parse_total_records(soup: BeautifulSoup) -> int:
    text = soup.get_text(" ", strip=True)
    match = SHOW_RE.search(text)
    if not match:
        return 0
    return int(match.group(3))


def parse_lot_table(soup: BeautifulSoup) -> list[list[str]]:
    for table in soup.select("table"):
        headers = [clean_text(h.get_text(" ", strip=True)) for h in table.select("thead th")]
        if headers and headers[:7] == [
            "№ лота",
            "Наименование объявления",
            "Наименование и описание лота",
            "Кол-во",
            "Сумма, тг.",
            "Способ закупки",
            "Статус",
        ]:
            rows: list[list[str]] = []
            for tr in table.select("tbody tr"):
                tds = tr.select("td")
                if len(tds) < 7:
                    continue
                first = clean_text(tds[0].get_text(" ", strip=True))
                if not first or ("Нет" in first and "лот" not in first):
                    continue
                rows.append(
                    [
                        first,
                        _extract_cell_text(tds[1]),
                        _extract_cell_text(tds[2]),
                        clean_text(tds[3].get_text(" ", strip=True)),
                        clean_text(tds[4].get_text(" ", strip=True)),
                        clean_text(tds[5].get_text(" ", strip=True)),
                        clean_text(tds[6].get_text(" ", strip=True)),
                    ]
                )
            return rows
    return []


def build_params(code: str, year: str, status: str, amount_from: str, page: int) -> dict[str, str]:
    params: dict[str, str] = {
        "filter[enstru]": code,
        "filter[year]": year,
        "count_record": str(PER_PAGE),
        "page": str(page),
    }
    if status:
        params["filter[status][]"] = status
    if amount_from:
        params["filter[amount_from]"] = amount_from
    return params


def fetch_page(
    session: requests.Session,
    code: str,
    year: str,
    status: str,
    amount_from: str,
    page: int,
    timeout: int,
    retries: int,
) -> tuple[list[list[str]], int]:
    params = build_params(code, year, status, amount_from, page)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(SEARCH_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            rows = parse_lot_table(soup)
            total = parse_total_records(soup)
            return rows, total
        except Exception as exc:  # pylint: disable=broad-except
            last_error = exc
            sleep_for = (2 ** (attempt - 1)) + random.uniform(0.1, 0.6)
            time.sleep(sleep_for)
    raise RuntimeError(f"Failed to fetch code={code} page={page}: {last_error}") from last_error


def fetch_code_rows(
    tru: TruCode,
    year: str,
    status: str,
    amount_from: str,
    timeout: int,
    retries: int,
) -> tuple[str, list[LotRow], int]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    first_rows, total = fetch_page(
        session=session,
        code=tru.code,
        year=year,
        status=status,
        amount_from=amount_from,
        page=1,
        timeout=timeout,
        retries=retries,
    )
    pages = max(1, math.ceil(total / PER_PAGE)) if total else 1

    all_rows = list(first_rows)
    for page in range(2, pages + 1):
        page_rows, _ = fetch_page(
            session=session,
            code=tru.code,
            year=year,
            status=status,
            amount_from=amount_from,
            page=page,
            timeout=timeout,
            retries=retries,
        )
        all_rows.extend(page_rows)
        time.sleep(random.uniform(0.15, 0.35))

    mapped = [
        LotRow(
            lot_no=row[0],
            tru_code=tru.code,
            product_name=tru.name,
            announcement_name=row[1],
            lot_name_desc=row[2],
            qty=row[3],
            amount_tenge=row[4],
            procurement_method=row[5],
            status=row[6],
        )
        for row in all_rows
    ]
    return tru.code, mapped, total


def ensure_csv_with_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)


def append_rows(path: Path, rows: list[LotRow]) -> int:
    if not rows:
        return 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row.as_list())
    return len(rows)


def load_resume(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # pylint: disable=broad-except
        return {}


def save_resume(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def dedupe_raw_csv(raw_csv: Path, output_csv: Path, db_path: Path) -> tuple[int, int]:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE lots (
          lot_no TEXT NOT NULL,
          tru_code TEXT NOT NULL,
          product_name TEXT,
          announcement_name TEXT NOT NULL,
          lot_name_desc TEXT,
          qty TEXT,
          amount_tenge TEXT,
          procurement_method TEXT,
          status TEXT,
          PRIMARY KEY (lot_no, tru_code, announcement_name)
        )
        """
    )

    rows_before = 0
    with raw_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        batch: list[list[str]] = []
        for row in reader:
            if len(row) < 9:
                continue
            rows_before += 1
            batch.append(row[:9])
            if len(batch) >= 5000:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO lots (
                      lot_no, tru_code, product_name, announcement_name,
                      lot_name_desc, qty, amount_tenge, procurement_method, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                conn.commit()
                batch.clear()
        if batch:
            cur.executemany(
                """
                INSERT OR REPLACE INTO lots (
                  lot_no, tru_code, product_name, announcement_name,
                  lot_name_desc, qty, amount_tenge, procurement_method, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            conn.commit()

    count_row = cur.execute("SELECT COUNT(*) FROM lots").fetchone()
    rows_after = int(count_row[0]) if count_row else 0

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(CSV_HEADER)
        for row in cur.execute(
            """
            SELECT
              lot_no, tru_code, product_name, announcement_name,
              lot_name_desc, qty, amount_tenge, procurement_method, status
            FROM lots
            ORDER BY tru_code, lot_no
            """
        ):
            writer.writerow(row)
    conn.close()
    return rows_before, rows_after


def main() -> int:
    args = parse_args()
    codes_csv = Path(args.codes_csv_path)
    raw_csv = Path(args.raw_csv)
    output_csv = Path(args.output_csv)
    stats_json = Path(args.stats_json)
    resume_json = Path(args.resume_json)
    dedupe_db = output_csv.with_suffix(".dedupe.sqlite")

    if not codes_csv.exists():
        print(f"[INFO] Downloading TRU codes CSV to {codes_csv}", flush=True)
        download_codes_csv(args.codes_csv_url, codes_csv)

    tru_codes = read_tru_codes(codes_csv)
    if args.limit_codes > 0:
        tru_codes = tru_codes[: args.limit_codes]
    print(f"[INFO] Loaded TRU codes: {len(tru_codes)}", flush=True)

    resumed = load_resume(resume_json)
    processed_codes = set(resumed.get("processed_codes", []))
    failed_codes: dict[str, str] = resumed.get("failed_codes", {})
    per_code: dict[str, dict[str, int | str]] = resumed.get("per_code", {})
    pending_codes = [tru for tru in tru_codes if tru.code not in processed_codes]
    print(
        f"[INFO] Resume: already_processed={len(processed_codes)}, pending={len(pending_codes)}",
        flush=True,
    )

    ensure_csv_with_header(raw_csv)
    lock = threading.Lock()
    processed_since_checkpoint = 0
    raw_written = 0

    started_at = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                fetch_code_rows,
                tru,
                args.year,
                args.status,
                args.amount_from,
                args.request_timeout,
                args.retries,
            ): tru
            for tru in pending_codes
        }

        done = len(processed_codes)
        total_codes = len(tru_codes)
        for future in as_completed(futures):
            tru = futures[future]
            done += 1
            try:
                code, rows, found_total = future.result()
                with lock:
                    raw_written += append_rows(raw_csv, rows)
                    processed_codes.add(code)
                    per_code[code] = {
                        "tru_name": tru.name,
                        "rows_collected": len(rows),
                        "portal_total": found_total,
                    }
                    processed_since_checkpoint += 1
                    if processed_since_checkpoint >= args.checkpoint_every:
                        save_resume(
                            resume_json,
                            {
                                "processed_codes": sorted(processed_codes),
                                "failed_codes": failed_codes,
                                "per_code": per_code,
                            },
                        )
                        processed_since_checkpoint = 0
                if done % 25 == 0 or done == total_codes:
                    print(
                        f"[INFO] Progress {done}/{total_codes} codes, "
                        f"raw_rows_written={raw_written}",
                        flush=True,
                    )
            except Exception as exc:  # pylint: disable=broad-except
                with lock:
                    failed_codes[tru.code] = str(exc)
                    processed_since_checkpoint += 1
                    if processed_since_checkpoint >= args.checkpoint_every:
                        save_resume(
                            resume_json,
                            {
                                "processed_codes": sorted(processed_codes),
                                "failed_codes": failed_codes,
                                "per_code": per_code,
                            },
                        )
                        processed_since_checkpoint = 0
                print(f"[WARN] Failed code={tru.code}: {exc}", file=sys.stderr, flush=True)

    save_resume(
        resume_json,
        {
            "processed_codes": sorted(processed_codes),
            "failed_codes": failed_codes,
            "per_code": per_code,
        },
    )

    print("[INFO] Dedupe raw CSV via sqlite...", flush=True)
    rows_before_dedup, rows_after_dedup = dedupe_raw_csv(raw_csv, output_csv, dedupe_db)
    elapsed = round(time.time() - started_at, 2)
    stats = {
        "year": args.year,
        "status": args.status,
        "amount_from": args.amount_from,
        "tru_codes_total": len(tru_codes),
        "tru_codes_processed": len(processed_codes),
        "failed_codes_count": len(failed_codes),
        "failed_codes": failed_codes,
        "rows_before_dedup": rows_before_dedup,
        "rows_written": rows_after_dedup,
        "raw_rows_written_in_this_run": raw_written,
        "elapsed_seconds": elapsed,
        "per_code": per_code,
        "raw_csv": str(raw_csv),
        "output_csv": str(output_csv),
    }
    stats_json.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[DONE] rows_written={rows_after_dedup}, failed_codes={len(failed_codes)}, "
        f"output={output_csv}, stats={stats_json}, elapsed={elapsed}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
