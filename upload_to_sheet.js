const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const SHEET_URL =
  "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing";
const CSV_PATH = path.resolve(process.argv[2] || "lots_2025_final.csv");
const START_CELL = (process.argv[3] || "A1").toUpperCase();
const CLEAR_FIRST = (process.argv[4] || "").toLowerCase() === "clear";

function csvToTsv(csvText) {
  const rows = [];
  let cur = "";
  let row = [];
  let inQuotes = false;

  for (let i = 0; i < csvText.length; i += 1) {
    const ch = csvText[i];
    const next = i + 1 < csvText.length ? csvText[i + 1] : "";

    if (ch === '"') {
      if (inQuotes && next === '"') {
        cur += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (!inQuotes && ch === ",") {
      row.push(cur);
      cur = "";
      continue;
    }

    if (!inQuotes && (ch === "\n" || ch === "\r")) {
      if (ch === "\r" && next === "\n") {
        i += 1;
      }
      row.push(cur);
      rows.push(row);
      row = [];
      cur = "";
      continue;
    }

    cur += ch;
  }

  if (cur.length > 0 || row.length > 0) {
    row.push(cur);
    rows.push(row);
  }

  return rows.map((r) => r.join("\t")).join("\n");
}

function splitRows(tsv) {
  const lines = tsv.split("\n");
  const header = lines[0] || "";
  const data = lines.slice(1).filter((x) => x.length > 0);
  return { header, data };
}

async function waitEditor(page) {
  await page.waitForTimeout(5000);
  const candidates = [
    "role=grid",
    "div#waffle-grid-container",
    "div.docs-sheet-container",
  ];
  for (const sel of candidates) {
    try {
      await page.waitForSelector(sel, { timeout: 15000 });
      return;
    } catch (_err) {
      // try next selector
    }
  }
  throw new Error("Google Sheets editor not detected.");
}

async function pasteChunk(page, chunkTsv) {
  await page.keyboard.insertText(chunkTsv);
}

async function goToCellOrRange(page, value) {
  await page.keyboard.press("Control+g");
  await page.waitForTimeout(500);
  await page.keyboard.type(value);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(700);
}

async function run() {
  if (!fs.existsSync(CSV_PATH)) {
    throw new Error(`CSV not found: ${CSV_PATH}`);
  }
  const csv = fs.readFileSync(CSV_PATH, "utf8");
  const tsv = csvToTsv(csv.replace(/^\uFEFF/, ""));
  const { header, data } = splitRows(tsv);

  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-dev-shm-usage"],
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto(SHEET_URL, { waitUntil: "domcontentloaded", timeout: 120000 });
  await waitEditor(page);

  if (START_CELL === "A1" && CLEAR_FIRST) {
    // Clear a wide range to remove leftovers from previous uploads.
    await goToCellOrRange(page, "A1:I1000000");
    await page.keyboard.press("Backspace");
    await page.waitForTimeout(1200);
    // Second pass helps remove residual rows left by partial browser operations.
    await page.keyboard.press("Backspace");
    await page.waitForTimeout(1200);
  }

  await goToCellOrRange(page, START_CELL);

  // For chunk-sized files (~100k rows) a single paste is more reliable
  // than iterative sub-pastes with cursor movement.
  const totalRows = data.length + 1;
  const allLines = [header, ...data];
  const payload = allLines.join("\n");
  await pasteChunk(page, payload);

  const approxRows = totalRows;
  const waitMs = Math.min(10 * 60 * 1000, Math.max(30_000, Math.floor(approxRows / 200) * 1000));
  await page.waitForTimeout(waitMs);

  // Trigger save sync
  await page.keyboard.press("Control+s");
  await page.waitForTimeout(8000);

  await context.close();
  await browser.close();
  console.log(`Uploaded ${approxRows} rows from ${CSV_PATH}`);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
