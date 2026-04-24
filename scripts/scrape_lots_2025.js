const fs = require("fs");
const path = require("path");
const cheerio = require("cheerio");
const { request } = require("playwright");

const INPUT_CSV = process.env.INPUT_CSV || path.join(process.cwd(), "tru_codes.csv");
const OUTPUT_CSV = process.env.OUTPUT_CSV || path.join(process.cwd(), "lots_2025_result.csv");
const OUTPUT_TSV = process.env.OUTPUT_TSV || path.join(process.cwd(), "lots_2025_result.tsv");
const CONCURRENCY = Number(process.env.CONCURRENCY || 12);
const COUNT_RECORD = Number(process.env.COUNT_RECORD || 2000);
const YEAR = String(process.env.YEAR || 2025);
const USER_AGENT = process.env.USER_AGENT || "Mozilla/5.0";
const CODE_OFFSET = Number(process.env.CODE_OFFSET || 0);
const CODE_LIMIT = Number(process.env.CODE_LIMIT || 0);

function parseCsvLine(line) {
  const out = [];
  let i = 0;
  let cur = "";
  let inQuote = false;
  while (i < line.length) {
    const ch = line[i];
    if (inQuote) {
      if (ch === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 2;
        continue;
      }
      if (ch === '"') {
        inQuote = false;
        i += 1;
        continue;
      }
      cur += ch;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inQuote = true;
      i += 1;
      continue;
    }
    if (ch === ",") {
      out.push(cur);
      cur = "";
      i += 1;
      continue;
    }
    cur += ch;
    i += 1;
  }
  out.push(cur);
  return out;
}

function parseCodes(csvPath) {
  const raw = fs.readFileSync(csvPath, "utf8");
  const lines = raw.split(/\r?\n/).filter(Boolean);
  const rows = lines.slice(1).map(parseCsvLine);
  const dedup = new Map();
  for (const row of rows) {
    const code = (row[0] || "").trim();
    const name = (row[1] || "").trim();
    if (!code) continue;
    if (!dedup.has(code)) dedup.set(code, name);
  }
  return [...dedup.entries()].map(([code, name]) => ({ code, name }));
}

function buildUrl(code, page) {
  const params = new URLSearchParams({
    "filter[enstru]": code,
    "filter[year]": YEAR,
    count_record: String(COUNT_RECORD),
    page: String(page),
  });
  return `https://goszakup.gov.kz/ru/search/lots?${params.toString()}`;
}

function cleanText(value) {
  return (value || "")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parseAnnouncement(text) {
  const parts = (text || "")
    .split(/\n+/)
    .map((x) => cleanText(x))
    .filter(Boolean)
    .filter((x) => !/^Заказчик:/i.test(x));
  return parts[0] || "";
}

function parseLotDescription(text) {
  const parts = (text || "")
    .split(/\n+/)
    .map((x) => cleanText(x))
    .filter(Boolean)
    .filter((x) => x !== "История");
  return parts.join(" ");
}

function escapeCsv(value) {
  const v = String(value ?? "");
  if (/[",\n]/.test(v)) {
    return `"${v.replace(/"/g, '""')}"`;
  }
  return v;
}

function toTsvSafe(value) {
  return String(value ?? "").replace(/\t/g, " ").replace(/\r?\n/g, " ");
}

function extractMaxPage($) {
  let maxPage = 1;
  $("a[href*='page=']").each((_, el) => {
    const href = $(el).attr("href") || "";
    const m = href.match(/[?&]page=(\d+)/);
    if (!m) return;
    const p = Number(m[1]);
    if (Number.isFinite(p) && p > maxPage) maxPage = p;
  });
  return maxPage;
}

async function fetchWithRetry(ctx, url, retries = 5) {
  let lastErr;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    try {
      return await ctx.get(url, { timeout: 60000 });
    } catch (err) {
      lastErr = err;
      const waitMs = 700 * 2 ** attempt;
      await new Promise((r) => setTimeout(r, waitMs));
    }
  }
  throw lastErr;
}

function parseRowsFromHtml(html, code, name) {
  const $ = cheerio.load(html);
  const rows = [];
  $("#search-result tbody tr").each((_, tr) => {
    const tds = $(tr).find("td");
    if (tds.length < 7) return;
    const lotNo = cleanText(tds.eq(0).text());
    if (!lotNo || lotNo === "Ничего не найдено") return;
    const announcement = parseAnnouncement(tds.eq(1).text());
    const lotDescription = parseLotDescription(tds.eq(2).text());
    const quantity = cleanText(tds.eq(3).text());
    const amount = cleanText(tds.eq(4).text());
    const method = cleanText(tds.eq(5).text());
    const status = cleanText(tds.eq(6).text());
    rows.push([lotNo, code, name, announcement, lotDescription, quantity, amount, method, status]);
  });
  return { rows, maxPage: extractMaxPage($) };
}

async function scrapeCode(ctx, item) {
  const firstUrl = buildUrl(item.code, 1);
  const firstRes = await fetchWithRetry(ctx, firstUrl);
  const firstHtml = await firstRes.text();
  const parsedFirst = parseRowsFromHtml(firstHtml, item.code, item.name);
  const allRows = [...parsedFirst.rows];
  if (parsedFirst.maxPage > 1) {
    for (let page = 2; page <= parsedFirst.maxPage; page += 1) {
      const nextRes = await fetchWithRetry(ctx, buildUrl(item.code, page));
      const nextHtml = await nextRes.text();
      const parsedNext = parseRowsFromHtml(nextHtml, item.code, item.name);
      allRows.push(...parsedNext.rows);
    }
  }
  return allRows;
}

async function main() {
  const allCodes = parseCodes(INPUT_CSV);
  const codes = allCodes.slice(CODE_OFFSET, CODE_LIMIT > 0 ? CODE_OFFSET + CODE_LIMIT : undefined);
  console.log(
    `Loaded TRU codes: ${allCodes.length}. Processing slice: offset=${CODE_OFFSET}, limit=${
      CODE_LIMIT > 0 ? CODE_LIMIT : "all"
    }, actual=${codes.length}`
  );
  const csvWs = fs.createWriteStream(OUTPUT_CSV, { encoding: "utf8" });
  const tsvWs = fs.createWriteStream(OUTPUT_TSV, { encoding: "utf8" });
  const header = [
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
  csvWs.write(`${header.map(escapeCsv).join(",")}\n`);
  tsvWs.write(`${header.map(toTsvSafe).join("\t")}\n`);

  const ctx = await request.newContext({
    extraHTTPHeaders: { "User-Agent": USER_AGENT },
  });

  let idx = 0;
  let processed = 0;
  let failed = 0;
  let totalRows = 0;
  const failures = [];
  const lock = { writing: Promise.resolve() };

  async function writeRows(rows) {
    if (!rows.length) return;
    const csvChunk = rows.map((r) => r.map(escapeCsv).join(",")).join("\n") + "\n";
    const tsvChunk = rows.map((r) => r.map(toTsvSafe).join("\t")).join("\n") + "\n";
    lock.writing = lock.writing.then(
      () =>
        new Promise((resolve, reject) => {
          csvWs.write(csvChunk, (err) => {
            if (err) return reject(err);
            tsvWs.write(tsvChunk, (err2) => (err2 ? reject(err2) : resolve()));
          });
        })
    );
    await lock.writing;
  }

  async function worker() {
    while (true) {
      const myIdx = idx;
      idx += 1;
      if (myIdx >= codes.length) return;
      const item = codes[myIdx];
      try {
        const rows = await scrapeCode(ctx, item);
        await writeRows(rows);
        totalRows += rows.length;
      } catch (err) {
        failed += 1;
        failures.push({ code: item.code, error: String(err.message || err) });
      } finally {
        processed += 1;
        if (processed % 50 === 0 || processed === codes.length) {
          console.log(
            `Progress: ${processed}/${codes.length}, rows=${totalRows}, failed=${failed}`
          );
        }
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));
  await lock.writing;
  await ctx.dispose();

  await new Promise((r) => csvWs.end(r));
  await new Promise((r) => tsvWs.end(r));

  const report = {
    input_csv: INPUT_CSV,
    processed_codes: processed,
    failed_codes: failed,
    total_rows: totalRows,
    output_csv: OUTPUT_CSV,
    output_tsv: OUTPUT_TSV,
    failures,
    code_offset: CODE_OFFSET,
    code_limit: CODE_LIMIT,
    concurrency: CONCURRENCY,
    count_record: COUNT_RECORD,
  };
  const reportPath = path.join(process.cwd(), "lots_2025_report.json");
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), "utf8");
  console.log(`Done. Rows: ${totalRows}. Failed codes: ${failed}. Report: ${reportPath}`);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
