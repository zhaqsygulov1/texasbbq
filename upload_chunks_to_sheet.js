const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const SHEET_URL =
  'https://docs.google.com/spreadsheets/d/13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU/edit?usp=sharing';
const MANIFEST_PATH = '/workspace/upload_chunks/manifest.json';
const STATE_PATH = '/workspace/upload_chunks/upload_state.json';
const START_CHUNK = Number(process.env.START_CHUNK || '1');
const MAX_CHUNKS = Number(process.env.MAX_CHUNKS || '0');

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function gotoCell(page, a1Ref) {
  const nameBox = page.locator('#t-name-box');
  await nameBox.fill(a1Ref);
  await nameBox.press('Enter');
  await sleep(500);
}

async function pasteText(page, text) {
  await page.evaluate(async (value) => {
    await navigator.clipboard.writeText(value);
  }, text);
  await page.keyboard.press('Control+v');
  await sleep(1800);
}

async function clearSheet(page) {
  // First Ctrl+A selects current block, second Ctrl+A selects entire sheet.
  await page.keyboard.press('Control+a');
  await sleep(250);
  await page.keyboard.press('Control+a');
  await sleep(250);
  await page.keyboard.press('Delete');
  await sleep(1500);
}

async function run() {
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const chunks = manifest.chunks;
  const state = fs.existsSync(STATE_PATH)
    ? JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'))
    : { completed_chunks: 0 };

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1700, height: 1200 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await context.newPage();
  await page.goto(SHEET_URL, { waitUntil: 'domcontentloaded', timeout: 120000 });
  await sleep(8000);

  // If this is a fresh run, clear current sheet.
  if (!state.completed_chunks) {
    await clearSheet(page);
  }

  for (const chunk of chunks) {
    if (chunk.index <= state.completed_chunks) {
      continue;
    }
    if (chunk.index < START_CHUNK) {
      continue;
    }
    if (MAX_CHUNKS > 0 && chunk.index >= START_CHUNK + MAX_CHUNKS) {
      break;
    }
    const a1Ref = `A${chunk.start_row}`;
    const text = fs.readFileSync(chunk.path, 'utf8');
    await gotoCell(page, a1Ref);
    await pasteText(page, text);

    state.completed_chunks = chunk.index;
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
    if (chunk.index % 5 === 0 || chunk.index === chunks.length) {
      console.log(`Uploaded chunk ${chunk.index}/${chunks.length}`);
      await page.screenshot({
        path: `/workspace/upload_chunks/progress_${String(chunk.index).padStart(4, '0')}.png`,
        fullPage: true,
      });
    }
  }

  await sleep(5000);
  await page.screenshot({ path: '/workspace/upload_chunks/final_sheet.png', fullPage: true });
  await browser.close();
  console.log('UPLOAD_DONE');
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
