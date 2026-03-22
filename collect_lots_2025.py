#!/usr/bin/env python3
"""Collect 2025 goszakup lots by TRU codes from Google Sheets.

Usage example:
python3 collect_lots_2025.py \
  --source-sheet-id 1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k \
  --output-csv output/lots_2025.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://goszakup.gov.kz/ru/search/lots"
DEFAULT_SOURCE_SHEET_ID = "1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k"
DEFAULT_SOURCE_GID = "0"


@dataclass(frozen=True)
class TruCode:
    code: str
    title: str


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def read_tru_codes(sheet_id: str, gid: str) -> List[TruCode]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    response = requests.get(
        url,
        params={"format": "csv", "gid": gid},
        timeout=60,
    )
    response.raise_for_status()

    content = response.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))

    seen: set[str] = set()
    result: List[TruCode] = []
    for row in reader:
        code = normalize_space(row.get("Код ТРУ", ""))
        title = normalize_space(row.get("Название", ""))
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append(TruCode(code=code, title=title))
    return result


def fetch_html_with_curl(url: str, retries: int = 4, sleep_seconds: float = 1.5) -> str:
    last_error: str | None = None
    for attempt in range(1, retries + 1):
        proc = subprocess.run(
            [
                "curl",
                "-LsS",
                "--connect-timeout",
                "25",
                "--max-time",
                "90",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        last_error = proc.stderr.strip() or f"curl exit code {proc.returncode}"
        if attempt < retries:
            time.sleep(sleep_seconds * attempt)

    raise RuntimeError(f"Failed to fetch URL after retries: {url}. Last error: {last_error}")


def build_search_url(
    tru_code: str,
    year: int,
    amount_from: int | None,
    status_id: str | None,
    count_record: int,
    page: int,
) -> str:
    params = {
        "filter[enstru]": tru_code,
        "filter[year]": str(year),
        "count_record": str(count_record),
        "page": str(page),
        "smb": "",
    }
    if amount_from is not None:
        params["filter[amount_from]"] = str(amount_from)
    if status_id:
        params["filter[status][]"] = status_id
    return f"{SEARCH_URL}?{urlencode(params)}"


def parse_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.select("ul.pagination li a[href]"):
        href = a.get("href", "")
        match = re.search(r"[?&]page=(\d+)", href)
        if not match:
            continue
        page = int(match.group(1))
        if page > max_page:
            max_page = page
    return max_page


def parse_rows(
    soup: BeautifulSoup,
    tru_code: str,
    tru_title: str,
) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in soup.select("#search-result tbody tr"):
        tds = tr.select("td")
        if len(tds) < 7:
            continue

        lot_number = normalize_space(tds[0].get_text(" ", strip=True))

        announce_anchor = tds[1].select_one("a")
        announcement_name = normalize_space(announce_anchor.get_text(" ", strip=True)) if announce_anchor else ""
        if not announcement_name:
            announcement_name = normalize_space(
                tds[1].get_text(" ", strip=True).split("Заказчик:")[0]
            )

        lot_anchor = tds[2].select_one("a")
        lot_name_description = normalize_space(lot_anchor.get_text(" ", strip=True)) if lot_anchor else ""
        if not lot_name_description:
            lot_name_description = normalize_space(
                tds[2].get_text(" ", strip=True).replace("История", "")
            )

        qty = normalize_space(tds[3].get_text(" ", strip=True))
        amount = normalize_space(tds[4].get_text(" ", strip=True))
        method = normalize_space(tds[5].get_text(" ", strip=True))
        status = normalize_space(tds[6].get_text(" ", strip=True))

        if not lot_number:
            continue

        rows.append(
            [
                lot_number,
                tru_code,
                tru_title,
                announcement_name,
                lot_name_description,
                qty,
                amount,
                method,
                status,
            ]
        )
    return rows


def collect_for_code(
    code: TruCode,
    year: int,
    amount_from: int | None,
    status_id: str | None,
    count_record: int,
    pause_seconds: float,
) -> List[List[str]]:
    out: List[List[str]] = []

    first_url = build_search_url(
        tru_code=code.code,
        year=year,
        amount_from=amount_from,
        status_id=status_id,
        count_record=count_record,
        page=1,
    )
    html = fetch_html_with_curl(first_url)
    soup = BeautifulSoup(html, "lxml")

    out.extend(parse_rows(soup, code.code, code.title))
    max_page = parse_max_page(soup)

    for page in range(2, max_page + 1):
        if pause_seconds > 0:
            time.sleep(pause_seconds)
        url = build_search_url(
            tru_code=code.code,
            year=year,
            amount_from=amount_from,
            status_id=status_id,
            count_record=count_record,
            page=page,
        )
        html = fetch_html_with_curl(url)
        soup = BeautifulSoup(html, "lxml")
        out.extend(parse_rows(soup, code.code, code.title))

    return out


def load_done_codes(progress_file: Path) -> set[str]:
    if not progress_file.exists():
        return set()
    done = set()
    for line in progress_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            done.add(line)
    return done


def append_done_code(progress_file: Path, code: str, lock: threading.Lock) -> None:
    with lock:
        with progress_file.open("a", encoding="utf-8") as f:
            f.write(code + "\n")


def load_existing_keys(output_csv: Path) -> set[tuple[str, str]]:
    if not output_csv.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    with output_csv.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lot = normalize_space(row.get("№ лота", ""))
            code = normalize_space(row.get("Код ТРУ", ""))
            if lot and code:
                keys.add((lot, code))
    return keys


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sheet-id", default=DEFAULT_SOURCE_SHEET_ID)
    parser.add_argument("--source-gid", default=DEFAULT_SOURCE_GID)
    parser.add_argument("--output-csv", default="output/lots_2025.csv")
    parser.add_argument("--progress-file", default="output/processed_codes.txt")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--status-id", default="360", help="goszakup status filter id (default: 360)")
    parser.add_argument(
        "--amount-from",
        type=int,
        default=15_000_000,
        help="minimum amount filter; use -1 to disable",
    )
    parser.add_argument("--count-record", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    parser.add_argument("--code-limit", type=int, default=0, help="for test runs only")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path = Path(args.progress_file)
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    amount_from: int | None = args.amount_from
    if amount_from is not None and amount_from < 0:
        amount_from = None

    status_id: str | None = args.status_id.strip() if args.status_id else None

    print("Loading TRU codes from source sheet...")
    codes = read_tru_codes(args.source_sheet_id, args.source_gid)
    if args.code_limit > 0:
        codes = codes[: args.code_limit]
    print(f"Loaded {len(codes)} unique TRU codes.")

    done_codes: set[str] = set()
    if args.resume:
        done_codes = load_done_codes(progress_path)
        print(f"Resume mode: {len(done_codes)} codes already marked as processed.")
    else:
        if progress_path.exists():
            progress_path.unlink()

    pending_codes = [c for c in codes if c.code not in done_codes]
    print(f"Pending codes: {len(pending_codes)}")
    if not pending_codes:
        print("Nothing to do.")
        return 0

    csv_mode = "a" if args.resume and output_path.exists() else "w"
    write_header = csv_mode == "w"

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

    progress_lock = threading.Lock()
    started = time.time()
    completed = 0
    total_rows = 0
    seen_keys: set[tuple[str, str]] = set()

    if args.resume and output_path.exists():
        seen_keys = load_existing_keys(output_path)
        print(f"Resume mode: loaded {len(seen_keys)} existing lot+code keys for dedupe.")

    with output_path.open(csv_mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_map = {
                executor.submit(
                    collect_for_code,
                    code,
                    args.year,
                    amount_from,
                    status_id,
                    args.count_record,
                    args.pause_seconds,
                ): code
                for code in pending_codes
            }

            for future in as_completed(future_map):
                code = future_map[future]
                completed += 1
                try:
                    rows = future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[{completed}/{len(pending_codes)}] ERROR for {code.code}: {exc}", file=sys.stderr)
                    continue

                unique_rows: List[List[str]] = []
                for row in rows:
                    key = (row[0], row[1])
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    unique_rows.append(row)

                if rows:
                    writer.writerows(unique_rows)
                    total_rows += len(unique_rows)
                    f.flush()

                append_done_code(progress_path, code.code, progress_lock)

                elapsed = max(1.0, time.time() - started)
                speed = completed / elapsed
                print(
                    f"[{completed}/{len(pending_codes)}] {code.code}: {len(unique_rows)} rows "
                    f"(total rows: {total_rows}, {speed:.2f} codes/s)"
                )

    total_elapsed = time.time() - started
    print(
        f"Done. Wrote {total_rows} rows to {output_path} in {total_elapsed:.1f} sec. "
        f"Progress file: {progress_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
