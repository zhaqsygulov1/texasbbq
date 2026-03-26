#!/usr/bin/env node
/*
Upload lots_2025_final.csv data to target Google Sheet by anonymous browser session.
It creates a new sheet tab, writes header+rows in chunks via clipboard paste, and
captures resulting gid for verification/export.
*/

const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const SHEET_URL =
  "https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing";
const CHUNKS_META_PATH = "/workspace/upload_chunks/meta.json";
const CHUNKS_DIR = "/workspace/upload_chunks";
const CHROME_PATH = "/usr/local/bin/google-chrome";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function clickGridCell(page, rowIndexOneBased) {
  // Use name box to jump directly to A{row}.
  const nameBox = await page.$("#t-name-box");
  if (!nameBox) {
    throw new Error("Could not locate name box #t-name-box");
  }
  await nameBox.click({ clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.keyboard.type(`A${rowIndexOneBased}`);
  await page.keyboard.press("Enter");
  await sleep(700);
}

async function pasteTsvAtRow(page, tsvText, rowIndexOneBased) {
  await clickGridCell(page, rowIndexOneBased);
  await page.evaluate(async (text) => {
    await navigator.clipboard.writeText(text);
  }, tsvText);
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyV");
  await page.keyboard.up("Control");
  await sleep(2200);
}

async function main() {
  if (!fs.existsSync(CHUNKS_META_PATH)) {
    throw new Error(`Missing chunks meta file: ${CHUNKS_META_PATH}`);
  }
  const meta = JSON.parse(fs.readFileSync(CHUNKS_META_PATH, "utf-8"));
  if (!Array.isArray(meta) || meta.length === 0) {
    throw new Error("Chunks meta is empty.");
  }

  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  page.setDefaultTimeout(180000);

  await page.goto(SHEET_URL, { waitUntil: "networkidle2" });
  await sleep(5000);

  const addBtn = await page.$(".docs-sheet-add-button");
  if (!addBtn) {
    throw new Error("Could not find add-sheet button.");
  }
  await addBtn.click();
  await sleep(4000);

  const gid = await page.evaluate(() => {
    const u = new URL(location.href);
    return u.searchParams.get("gid") || location.hash.replace("#gid=", "");
  });
  if (!gid) {
    throw new Error("Failed to detect created sheet gid.");
  }

  for (const chunk of meta) {
    const tsvPath = path.join(CHUNKS_DIR, `chunk_${String(chunk.chunk).padStart(3, "0")}.tsv`);
    if (!fs.existsSync(tsvPath)) {
      throw new Error(`Missing chunk file: ${tsvPath}`);
    }
    const tsv = fs.readFileSync(tsvPath, "utf-8");
    await pasteTsvAtRow(page, tsv, Number(chunk.start_row));
    console.log(
      `[uploaded] chunk=${chunk.chunk} rows=${chunk.data_rows} start_row=${chunk.start_row}`
    );
  }

  await sleep(9000);
  const finalUrl = page.url();
  fs.writeFileSync(
    "/workspace/upload_result.json",
    JSON.stringify({ gid, finalUrl, chunks: meta.length }, null, 2),
    "utf-8"
  );

  await page.screenshot({ path: "/workspace/upload_complete.png" });
  await browser.close();
  console.log(`[done] gid=${gid} chunks=${meta.length}`);
}

main().catch((err) => {
  console.error("[fatal]", err?.stack || err);
  process.exit(1);
});

