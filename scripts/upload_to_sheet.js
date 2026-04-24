const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const TARGET_URL =
  process.env.TARGET_URL ||
  "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing";
const TSV_PATH = process.env.TSV_PATH || path.join(process.cwd(), "lots_2025_result.tsv");
const TARGET_TAB = process.env.TARGET_TAB || "Лист17";
const CHUNK_LINES = Number(process.env.CHUNK_LINES || 3000);
const START_LINE = Number(process.env.START_LINE || 1);
const VERIFY_AFTER_PASTE = (process.env.VERIFY_AFTER_PASTE || "1") !== "0";
const MAX_CHUNK_RETRIES = Number(process.env.MAX_CHUNK_RETRIES || 4);

function splitLines(raw) {
  return raw.split(/\r?\n/).filter((line) => line.length > 0);
}

async function waitForGridId(page) {
  await page.waitForTimeout(1000);
  return page.evaluate(() => {
    const el = [...document.querySelectorAll("[id$='-grid-container']")].find(
      (x) => x.id !== "waffle-grid-container"
    );
    return el ? el.id : null;
  });
}

async function gotoCell(page, cellRef) {
  const nameBox = page.locator("#t-name-box");
  await page.keyboard.press("Escape").catch(() => {});
  await page.evaluate(() => {
    document.querySelectorAll(".modal-dialog-bg,.modal-dialog").forEach((e) => e.remove());
  });
  await nameBox.click({ timeout: 10000 });
  await nameBox.fill(cellRef);
  await nameBox.press("Enter");
  await page.waitForTimeout(900);
}

function firstCellFromChunk(chunkText) {
  const firstLine = chunkText.split("\n")[0] || "";
  return (firstLine.split("\t")[0] || "").trim();
}

async function main() {
  if (!fs.existsSync(TSV_PATH)) {
    throw new Error(`TSV file not found: ${TSV_PATH}`);
  }
  const lines = splitLines(fs.readFileSync(TSV_PATH, "utf8"));
  if (!lines.length) {
    throw new Error("TSV file is empty.");
  }
  console.log(`TSV lines loaded: ${lines.length}`);
  if (START_LINE < 1 || START_LINE > lines.length) {
    throw new Error(`Invalid START_LINE=${START_LINE}. Must be in range 1..${lines.length}`);
  }

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForTimeout(12000);

  await page.locator(".docs-sheet-tab-name", { hasText: TARGET_TAB }).first().click();
  await page.waitForTimeout(1500);

  const gridId = await waitForGridId(page);
  if (!gridId) {
    throw new Error("Could not detect active Google Sheets grid container.");
  }
  const grid = page.locator(`[id="${gridId}"]`);
  const formula = page.locator("#t-formula-bar-input");
  await grid.click({ position: { x: 90, y: 20 }, force: true });

  if (START_LINE === 1) {
    const clearTo = Math.max(lines.length + 20000, 800000);
    // Full refresh mode: clear a large area to avoid stale rows.
    await gotoCell(page, `A1:I${clearTo}`);
    await page.keyboard.press("Delete");
    await page.waitForTimeout(800);
  }

  let start = START_LINE - 1;
  while (start < lines.length) {
    const end = Math.min(start + CHUNK_LINES, lines.length);
    const chunk = lines.slice(start, end).join("\n");
    const rowStart = start + 1;

    const expectedA = firstCellFromChunk(chunk);
    let uploaded = false;
    let attempt = 0;
    while (!uploaded && attempt < MAX_CHUNK_RETRIES) {
      attempt += 1;
      await gotoCell(page, `A${rowStart}`);
      await grid.click({ position: { x: 90, y: 20 }, force: true });

      await page.evaluate(async (txt) => {
        await navigator.clipboard.writeText(txt);
      }, chunk);
      await page.keyboard.press("Control+V");
      await page.waitForTimeout(1200);

      if (!VERIFY_AFTER_PASTE) {
        uploaded = true;
        break;
      }

      await gotoCell(page, `A${rowStart}`);
      const actualA = (await formula.innerText()).trim();
      if (actualA === expectedA) {
        uploaded = true;
      } else {
        console.log(
          `Retry chunk ${rowStart}-${end}: expected A${rowStart}="${expectedA}" got "${actualA}" (attempt ${attempt})`
        );
      }
    }

    if (!uploaded) {
      throw new Error(`Failed to upload chunk ${rowStart}-${end} after ${MAX_CHUNK_RETRIES} attempts`);
    }

    console.log(`Uploaded lines: ${start + 1}-${end}`);
    start = end;
  }

  await page.waitForTimeout(2000);
  await browser.close();
  console.log("Upload completed.");
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
