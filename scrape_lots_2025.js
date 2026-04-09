const fs = require("fs");
const path = require("path");
const cheerio = require("cheerio");

const SOURCE_CSV_URL =
  "https://docs.google.com/spreadsheets/d/1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k/export?format=csv&gid=0";
const OUTPUT_CSV = path.join(process.cwd(), "lots_2025_result.csv");
const OUTPUT_JSON = path.join(process.cwd(), "lots_2025_result.json");
const PROGRESS_JSON = path.join(process.cwd(), "lots_2025_progress.json");

const YEAR = "2025";
const STATUS = "360"; // Закупка состоялась
const AMOUNT_FROM = "15000000";
const COUNT_PER_PAGE = 2000;
const MAX_RETRIES = 6;
const REQUEST_TIMEOUT_MS = 45000;
const CONCURRENCY = 6;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanText(value) {
  return (value || "").replace(/\s+/g, " ").trim();
}

function csvEscape(value) {
  const s = String(value ?? "");
  if (/[",\n]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function parseCsvTwoColumns(csvText) {
  const rows = [];
  const lines = csvText.split(/\r?\n/).filter((line) => line.length > 0);
  if (lines.length <= 1) return rows;

  // Простой CSV-парсер под 2 колонки: Код ТРУ,Название
  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i];
    const commaIndex = line.indexOf(",");
    if (commaIndex < 0) continue;
    const code = line.slice(0, commaIndex).trim();
    let name = line.slice(commaIndex + 1).trim();
    if (name.startsWith('"') && name.endsWith('"')) {
      name = name.slice(1, -1).replace(/""/g, '"');
    }
    if (!code) continue;
    rows.push({ code, itemName: name });
  }
  return rows;
}

async function fetchWithRetry(url, attemptHint = "") {
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        headers: { "user-agent": "Mozilla/5.0" },
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return await response.text();
    } catch (error) {
      clearTimeout(timer);
      const isLast = attempt === MAX_RETRIES;
      const reason =
        error?.cause?.code || error?.name || error?.message || "UNKNOWN";
      if (isLast) {
        throw new Error(
          `fetch failed ${attemptHint} after ${MAX_RETRIES} attempts: ${reason}`
        );
      }
      await sleep(700 * attempt);
    }
  }
  throw new Error(`unexpected retry loop exit ${attemptHint}`);
}

function buildSearchUrl(code, page) {
  const params = new URLSearchParams();
  params.set("filter[enstru]", code);
  params.append("filter[status][]", STATUS);
  params.set("filter[amount_from]", AMOUNT_FROM);
  params.set("filter[year]", YEAR);
  params.set("count_record", String(COUNT_PER_PAGE));
  params.set("page", String(page));
  params.set("smb", "");
  return `https://goszakup.gov.kz/ru/search/lots?${params.toString()}`;
}

function parseLotsPage(html, code, itemName) {
  const $ = cheerio.load(html);
  const infoText = cleanText($("div.dataTables_info strong").first().text());
  const totalMatch = infoText.match(/из\s+(\d+)\s+запис/i);
  const total = totalMatch ? Number(totalMatch[1]) : 0;

  const rows = [];
  $("#search-result tbody tr").each((_, tr) => {
    const tds = $(tr).find("td");
    if (tds.length < 7) return;

    const lotNumber = cleanText($(tds[0]).text());
    const announcementName = cleanText($(tds[1]).find("a").first().text());
    const lotNameAndDescription = cleanText($(tds[2]).find("a").first().text());
    const qty = cleanText($(tds[3]).text());
    const amount = cleanText($(tds[4]).text());
    const buyMethod = cleanText($(tds[5]).text());
    const status = cleanText($(tds[6]).text());

    if (!lotNumber) return;
    rows.push({
      "№ лота": lotNumber,
      "Код ТРУ": code,
      "Наименование товара": itemName,
      "Наименование объявления": announcementName,
      "Наименование и описание лота": lotNameAndDescription,
      "Кол-во": qty,
      "Сумма, тг.": amount,
      "Способ закупки": buyMethod,
      "Статус": status,
    });
  });

  return { total, rows, infoText };
}

async function fetchLotsByCode(code, itemName) {
  const allRows = [];
  const firstUrl = buildSearchUrl(code, 0);
  const firstHtml = await fetchWithRetry(firstUrl, `${code} page=0`);
  const firstParsed = parseLotsPage(firstHtml, code, itemName);
  allRows.push(...firstParsed.rows);

  const total = firstParsed.total;
  const pages = Math.ceil(total / COUNT_PER_PAGE);

  for (let page = 1; page < pages; page += 1) {
    const pageUrl = buildSearchUrl(code, page);
    const html = await fetchWithRetry(pageUrl, `${code} page=${page}`);
    const parsed = parseLotsPage(html, code, itemName);
    allRows.push(...parsed.rows);
  }

  return {
    total,
    pages,
    rows: allRows,
    infoText: firstParsed.infoText,
  };
}

async function runPool(items, worker, concurrency) {
  const results = new Array(items.length);
  let nextIndex = 0;

  async function runWorker() {
    while (true) {
      const idx = nextIndex;
      nextIndex += 1;
      if (idx >= items.length) return;
      results[idx] = await worker(items[idx], idx);
    }
  }

  const workers = [];
  for (let i = 0; i < concurrency; i += 1) {
    workers.push(runWorker());
  }
  await Promise.all(workers);
  return results;
}

function writeOutputs(rows) {
  const headers = [
    "№ лота",
    "Код ТРУ",
    "Наименование товара",
    "Наименование объявления",
    "Наименование и описание лота",
    "Кол-во",
    "Сумма, тг.",
    "Способ закупки",
    "Статус",
  ];
  const csvLines = [headers.map(csvEscape).join(",")];
  for (const row of rows) {
    csvLines.push(headers.map((h) => csvEscape(row[h] ?? "")).join(","));
  }
  fs.writeFileSync(OUTPUT_CSV, `${csvLines.join("\n")}\n`, "utf8");
  fs.writeFileSync(OUTPUT_JSON, JSON.stringify(rows, null, 2), "utf8");
}

async function main() {
  const sourceCsv = await fetchWithRetry(SOURCE_CSV_URL, "download source csv");
  const sourceRows = parseCsvTwoColumns(sourceCsv);

  const byCode = new Map();
  for (const row of sourceRows) {
    if (!byCode.has(row.code)) {
      byCode.set(row.code, row.itemName);
    }
  }

  const codes = [...byCode.entries()].map(([code, itemName]) => ({
    code,
    itemName,
  }));

  console.log(`Loaded ${sourceRows.length} rows, unique TRU codes: ${codes.length}`);

  const allRows = [];
  const errors = [];
  let processed = 0;
  let matchedCodes = 0;

  await runPool(
    codes,
    async ({ code, itemName }, idx) => {
      try {
        const result = await fetchLotsByCode(code, itemName);
        if (result.rows.length > 0) {
          matchedCodes += 1;
          allRows.push(...result.rows);
        }
      } catch (error) {
        errors.push({ code, error: String(error) });
      } finally {
        processed += 1;
        if (processed % 20 === 0 || processed === codes.length) {
          console.log(
            `Progress ${processed}/${codes.length}; matched codes: ${matchedCodes}; rows: ${allRows.length}; errors: ${errors.length}`
          );
          fs.writeFileSync(
            PROGRESS_JSON,
            JSON.stringify(
              {
                processed,
                totalCodes: codes.length,
                matchedCodes,
                rowsCollected: allRows.length,
                errors,
                sampleRows: allRows.slice(0, 10),
              },
              null,
              2
            ),
            "utf8"
          );
        }
      }
      // Небольшая пауза, чтобы не долбить сервер слишком агрессивно.
      if ((idx + 1) % 7 === 0) {
        await sleep(250);
      }
    },
    CONCURRENCY
  );

  const dedup = new Map();
  for (const row of allRows) {
    const key = `${row["№ лота"]}||${row["Код ТРУ"]}`;
    if (!dedup.has(key)) dedup.set(key, row);
  }
  const finalRows = [...dedup.values()].sort((a, b) =>
    String(a["№ лота"]).localeCompare(String(b["№ лота"]), "ru")
  );

  writeOutputs(finalRows);
  fs.writeFileSync(
    PROGRESS_JSON,
    JSON.stringify(
      {
        processed,
        totalCodes: codes.length,
        matchedCodes,
        rowsCollected: allRows.length,
        finalRows: finalRows.length,
        errors,
      },
      null,
      2
    ),
    "utf8"
  );

  console.log(
    `Done. Final rows: ${finalRows.length}. Files:\n- ${OUTPUT_CSV}\n- ${OUTPUT_JSON}\n- ${PROGRESS_JSON}`
  );
  if (errors.length > 0) {
    console.log(`Errors captured: ${errors.length}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

