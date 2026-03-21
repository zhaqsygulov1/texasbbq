#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

function parseArgs(argv) {
  const result = {};
  for (let i = 2; i < argv.length; i++) {
    const value = argv[i];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      result[key] = true;
    } else {
      result[key] = next;
      i += 1;
    }
  }
  return result;
}

async function gotoCell(page, ref) {
  const nameBox = page.locator("#t-name-box");
  for (let attempt = 1; attempt <= 8; attempt++) {
    try {
      await dismissBlockingOverlays(page);
      await nameBox.click({ timeout: 8000 });
      await nameBox.fill(ref);
      await page.keyboard.press("Enter");
      // Return keyboard focus to grid editor before paste.
      await page.keyboard.press("Escape");
      return;
    } catch (err) {
      if (attempt === 8) throw err;
      await dismissBlockingOverlays(page);
      await page.waitForTimeout(800);
    }
  }
}

async function clearSheet(page) {
  await gotoCell(page, "A1");
  await page.keyboard.press("Control+a");
  await page.keyboard.press("Control+a");
  await page.keyboard.press("Backspace");
  await page.waitForTimeout(2500);
}

async function dismissBlockingOverlays(page) {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);

  const buttonTexts = [
    "Got it",
    "OK",
    "Cancel",
    "Continue",
    "ОК",
    "Понятно",
    "Отмена",
    "Продолжить",
  ];

  for (const text of buttonTexts) {
    const button = page.locator(`button:has-text("${text}")`).first();
    if (await button.isVisible().catch(() => false)) {
      await button.click().catch(() => {});
      await page.waitForTimeout(250);
    }
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const sheetUrl = args.url;
  const tsvPath = args.tsv ? path.resolve(args.tsv) : null;
  const chunkRows = Number(args["chunk-rows"] || "1000");
  const waitMs = Number(args["wait-ms"] || "2200");
  const screenshotPath = path.resolve(args.screenshot || "sheet_upload_result.png");

  if (!sheetUrl || !tsvPath) {
    console.error("Usage: node upload_tsv_to_sheet.js --url <sheet_url> --tsv <file.tsv> [--chunk-rows 1000]");
    process.exit(1);
  }

  const rawTsv = fs.readFileSync(tsvPath, "utf8").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = rawTsv.split("\n").filter((line, idx, all) => !(idx === all.length - 1 && line === ""));
  console.log(`Loaded ${lines.length} lines from ${tsvPath}.`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: "https://docs.google.com",
  });
  const page = await context.newPage();
  await page.goto(sheetUrl, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForTimeout(10000);

  await clearSheet(page);
  console.log("Sheet cleared.");

  let startLine = 0;
  let startRow = 1;
  let chunkIndex = 0;
  while (startLine < lines.length) {
    chunkIndex += 1;
    const endLineExclusive = Math.min(startLine + chunkRows, lines.length);
    const linesInChunk = endLineExclusive - startLine;
    const chunk = lines.slice(startLine, endLineExclusive).join("\n");
    const cellRef = `A${startRow}`;

    await page.evaluate(async (text) => {
      await navigator.clipboard.writeText(text);
    }, chunk);

    await gotoCell(page, cellRef);
    await page.keyboard.press("Control+v");
    await page.waitForTimeout(waitMs);
    await dismissBlockingOverlays(page);

    console.log(
      `Pasted chunk ${chunkIndex} at ${cellRef} (${linesInChunk} lines)`
    );

    if (endLineExclusive >= lines.length) {
      break;
    }
    // Paste overlaps one row at the end so the next start row is always valid.
    startLine = endLineExclusive - 1;
    startRow = startRow + linesInChunk - 1;
  }

  await page.waitForTimeout(12000);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await browser.close();
  console.log(`Upload complete. Screenshot: ${screenshotPath}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
