#!/usr/bin/env python3
import argparse
import csv
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


TARGET_SHEET_URL = "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing"


def csv_to_tsv(csv_path: Path) -> str:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        lines = []
        for row in reader:
            safe = [cell.replace("\r", " ").replace("\n", " ").replace("\t", " ") for cell in row]
            lines.append("\t".join(safe))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузка CSV в Google Sheet через браузер.")
    parser.add_argument(
        "--csv",
        default="output/lots_2025_by_tru_status360_amount15m.csv",
        help="Путь к CSV файлу для загрузки.",
    )
    parser.add_argument("--url", default=TARGET_SHEET_URL, help="URL целевой Google-таблицы.")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV файл не найден: {csv_path}")
        return 1

    tsv_data = csv_to_tsv(csv_path)
    rows_count = tsv_data.count("\n")
    print(f"Подготовлено строк (включая заголовок): {rows_count + 1}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(10000)

            text = page.content()
            if "Вход" in text and "accounts.google.com" in page.url:
                print("Требуется авторизация Google. Анонимная запись недоступна.")
                return 2

            if "просмотр" in text.lower() and "доступ" in text.lower():
                print("Похоже, таблица в режиме просмотра. Нет прав на редактирование.")
                return 3

            try:
                # Try to click A1.
                page.click("div[role='gridcell']", timeout=30000)
            except PlaywrightTimeoutError:
                # Fallback: click the sheet body.
                page.mouse.click(300, 300)
            page.wait_for_timeout(1000)

            # Clear current sheet content.
            page.keyboard.press("Control+A")
            page.wait_for_timeout(300)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(1000)

            # Paste data.
            page.evaluate(
                """async (tsv) => {
                    await navigator.clipboard.writeText(tsv);
                }""",
                tsv_data,
            )
            page.keyboard.press("Control+V")
            page.wait_for_timeout(20000)

            # Small edit to ensure save trigger.
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(5000)

            print("Попытка вставки завершена. Проверьте целевую таблицу.")
            return 0
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
