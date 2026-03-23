#!/usr/bin/env node

const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer");

const SPREADSHEET_ID = "13U7JlDKWQlK64mzg4P9AXLs_QXQwkQvoC82x-9zHcrU";
const TARGET_GID = "2088501793";
const INPUT_JSONL = path.join(__dirname, "..", "data", "lots_2025.jsonl");
const CHUNK_COMMANDS = 600;
const FLUSH_DELAY_MS = 1500;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseMultipartFields(body) {
  const extract = (name) => {
    const regex = new RegExp(
      `name="${name}"\\r\\n\\r\\n([\\s\\S]*?)\\r\\n------`,
      "m"
    );
    const match = body.match(regex);
    return match ? match[1].trim() : undefined;
  };
  return {
    rev: extract("rev"),
    bundles: extract("bundles"),
    selection: extract("selection"),
  };
}

function parseSaveResponse(rawText) {
  const text = rawText.startsWith(")]}'")
    ? rawText.slice(rawText.indexOf("\n") + 1)
    : rawText;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function buildCellCommand(sheetId, rowIndex, colIndex, value) {
  const cmd = [
    [String(sheetId), rowIndex, rowIndex + 1, colIndex, colIndex + 1],
    [132274236, 3, [2, value], null, null, 0],
    [null, [[null, 513, [0], null, null, null, null, null, null, null, null, 0]]],
  ];
  return [21299578, JSON.stringify(cmd)];
}

function loadRows(jsonlPath) {
  const content = fs.readFileSync(jsonlPath, "utf8");
  const lines = content.split(/\r?\n/).filter(Boolean);
  return lines.map((line) => JSON.parse(line));
}

async function main() {
  if (!fs.existsSync(INPUT_JSONL)) {
    throw new Error(`Input file not found: ${INPUT_JSONL}`);
  }

  const rows = loadRows(INPUT_JSONL);
  let startRow = 0;
  let endRowExclusive = rows.length;
  for (let i = 2; i < process.argv.length; i += 1) {
    const arg = process.argv[i];
    const value = process.argv[i + 1];
    if (arg === "--start-row" && value) {
      startRow = Number(value);
    }
    if (arg === "--end-row" && value) {
      endRowExclusive = Number(value);
    }
  }
  if (!Number.isInteger(startRow) || startRow < 0) startRow = 0;
  if (!Number.isInteger(endRowExclusive) || endRowExclusive > rows.length) endRowExclusive = rows.length;
  if (endRowExclusive < startRow) endRowExclusive = startRow;

  console.log(`Rows in file: ${rows.length}`);
  console.log(`Uploading range: [${startRow}, ${endRowExclusive})`);

  const browser = await puppeteer.launch({
    headless: "new",
    executablePath: "/usr/local/bin/google-chrome",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(120000);

    let capturedSave = null;
    page.on("request", (req) => {
      if (capturedSave) return;
      if (req.method() === "POST" && req.url().includes("/save")) {
        capturedSave = {
          url: req.url(),
          body: req.postData() || "",
        };
      }
    });

    await page.goto(
      `https://docs.google.com/spreadsheets/d/${SPREADSHEET_ID}/edit?gid=${TARGET_GID}#gid=${TARGET_GID}`,
      { waitUntil: "networkidle2", timeout: 120000 }
    );
    await sleep(9000);

    // Bootstrap one native save to capture URL/form fields (sid/rev/selection).
    await page.keyboard.type("BOOTSTRAP_UPLOAD");
    await page.keyboard.press("Enter");
    await sleep(4000);

    if (!capturedSave) {
      throw new Error("Could not capture native /save request.");
    }

    const fields = parseMultipartFields(capturedSave.body);
    if (!fields.rev || !fields.selection || !fields.bundles) {
      console.error("Captured body preview:", capturedSave.body.slice(0, 1200));
      throw new Error("Failed to parse required fields from captured /save request.");
    }

    const capturedBundles = JSON.parse(fields.bundles);
    const sid = capturedBundles[0].sid;
    let rev = Number(fields.rev);
    const selection = fields.selection;
    const saveUrl = capturedSave.url;

    let reqId = 1000;
    let commandsBatch = [];
    let processedRows = 0;

    const flush = async () => {
      if (!commandsBatch.length) return;
      const bundles = [
        {
          commands: commandsBatch,
          sid,
          reqId: reqId++,
        },
      ];

      let result = null;
      let attempt = 0;
      while (attempt < 20) {
        attempt += 1;
        result = await page.evaluate(
          async ({ url, revValue, selectionValue, bundlesValue }) => {
            const form = new FormData();
            form.append("rev", String(revValue));
            form.append("bundles", JSON.stringify(bundlesValue));
            form.append("selection", selectionValue);
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 45000);
            try {
              const response = await fetch(url, {
                method: "POST",
                body: form,
                credentials: "include",
                signal: controller.signal,
              });
              const text = await response.text();
              return { status: response.status, text };
            } catch (error) {
              return { status: 0, text: String(error) };
            } finally {
              clearTimeout(timer);
            }
          },
          {
            url: saveUrl,
            revValue: rev,
            selectionValue: selection,
            bundlesValue: bundles,
          }
        );

        if (result.status === 200) {
          break;
        }
        if (result.status === 550 || result.status >= 500) {
          console.log(`Chunk retry ${attempt} due status ${result.status}`);
          await sleep(Math.min(60000, 5000 * attempt));
          continue;
        }
        if (result.status === 0) {
          console.log(`Chunk retry ${attempt} due network timeout/error`);
          await sleep(Math.min(60000, 5000 * attempt));
          continue;
        }
        break;
      }

      if (!result || result.status !== 200) {
        const snippet = result ? result.text.slice(0, 300) : "no response";
        throw new Error(`Save request failed with status ${result?.status}. Body: ${snippet}`);
      }

      const payload = parseSaveResponse(result.text);
      if (payload && payload.metadata && Number.isInteger(payload.metadata.serverRevision)) {
        rev = payload.metadata.serverRevision;
      } else {
        rev += 1;
      }
      commandsBatch = [];
      await sleep(FLUSH_DELAY_MS);
    };

    for (let rowIndex = startRow; rowIndex < endRowExclusive; rowIndex++) {
      const row = rows[rowIndex];
      for (let colIndex = 0; colIndex < row.length; colIndex++) {
        commandsBatch.push(buildCellCommand(TARGET_GID, rowIndex, colIndex, String(row[colIndex] ?? "")));
        if (commandsBatch.length >= CHUNK_COMMANDS) {
          await flush();
        }
      }
      processedRows += 1;
      if (processedRows % 100 === 0) {
        console.log(`Uploaded rows in range: ${processedRows}/${endRowExclusive - startRow}`);
      }
    }

    await flush();
    console.log(`Upload completed for range [${startRow}, ${endRowExclusive})`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
