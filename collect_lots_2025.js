const fs = require('fs');
const path = require('path');
const { load } = require('cheerio');
const { parse } = require('csv-parse/sync');

const SOURCE_SHEET_ID = '1rAeiw4xFWV6XjT1uDFka95hbMFtsxuN3hxuuMGOPz7k';
const SOURCE_CSV_URL = `https://docs.google.com/spreadsheets/d/${SOURCE_SHEET_ID}/gviz/tq?tqx=out:csv`;

const OUTPUT_HEADERS = [
  '№ лота',
  'Код ТРУ',
  'Наименование товара',
  'Наименование объявления',
  'Наименование и описание лота',
  'Кол-во',
  'Сумма, тг.',
  'Способ закупки',
  'Статус',
];

const USER_AGENT =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36';

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeText(value) {
  return String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function csvEscape(value) {
  const str = String(value ?? '').replace(/\r?\n/g, ' ').trim();
  if (/[",\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function toCsvLine(values) {
  return `${values.map(csvEscape).join(',')}\n`;
}

function getArgValue(name, fallback) {
  const prefix = `--${name}=`;
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  if (!hit) return fallback;
  return hit.slice(prefix.length);
}

function buildSearchUrl({ code, year, countRecord, page }) {
  const params = new URLSearchParams();
  params.set('filter[enstru]', code);
  params.set('filter[year]', String(year));
  params.set('count_record', String(countRecord));
  params.set('page', String(page));
  params.set('smb', '');
  return `https://goszakup.gov.kz/ru/search/lots?${params.toString()}`;
}

async function fetchTextWithRetry(url, retries = 6) {
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { 'user-agent': USER_AGENT },
        redirect: 'follow',
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return await response.text();
    } catch (error) {
      if (attempt === retries) {
        throw error;
      }
      const backoffMs = Math.min(20000, 1000 * 2 ** (attempt - 1));
      await sleep(backoffMs);
    }
  }
  throw new Error('Unreachable fetch retry state');
}

function parseSourceRows(csvText) {
  const records = parse(csvText, {
    columns: true,
    skip_empty_lines: true,
    relax_column_count: true,
  });

  const sample = records[0] || {};
  const keys = Object.keys(sample);
  const codeKey = keys.find((key) => key.toLowerCase().includes('код')) || keys[0];
  const nameKey =
    keys.find((key) => key.toLowerCase().includes('назв')) ||
    keys.find((key) => key !== codeKey) ||
    keys[1];

  const map = new Map();
  for (const record of records) {
    const code = normalizeText(record[codeKey]);
    const productName = normalizeText(record[nameKey]);
    if (!code || map.has(code)) continue;
    map.set(code, { code, productName });
  }
  return [...map.values()];
}

function parsePageInfo($) {
  const infoText = normalizeText($('.dataTables_info strong').first().text());
  const match = infoText.match(/из\s*([\d\s]+)\s*записей/i);
  if (!match) return null;
  const total = Number(match[1].replace(/\s+/g, ''));
  if (!Number.isFinite(total)) return null;
  return total;
}

function parseLotRows($) {
  const rows = [];
  $('#search-result tbody tr').each((_, tr) => {
    const cells = $(tr).find('td');
    if (cells.length < 7) return;

    const lotNumber = normalizeText(cells.eq(0).find('strong').first().text() || cells.eq(0).text());
    const announceName = normalizeText(
      cells.eq(1).find('a strong').first().text() || cells.eq(1).find('a').first().text() || cells.eq(1).text(),
    );

    const lotName = normalizeText(
      cells.eq(2).find('a strong').first().text() || cells.eq(2).find('a').first().text() || cells.eq(2).text(),
    );
    const lotDescRaw = normalizeText(cells.eq(2).find('small.hidden-xs').first().text());
    const lotDesc = lotDescRaw && lotDescRaw !== 'История' ? `${lotName} | ${lotDescRaw}` : lotName;

    const quantity = normalizeText(cells.eq(3).text());
    const amount = normalizeText(cells.eq(4).text());
    const method = normalizeText(cells.eq(5).text());
    const status = normalizeText(cells.eq(6).text());

    if (!lotNumber) return;
    rows.push({ lotNumber, announceName, lotDesc, quantity, amount, method, status });
  });
  return rows;
}

async function collectByCode({ code, productName, year, countRecord }) {
  let page = 1;
  let totalPages = 1;
  let totalRecords = 0;
  let firstPage = true;
  let rowsCsv = '';
  let parsedRows = 0;

  while (page <= totalPages) {
    const url = buildSearchUrl({ code, year, countRecord, page });
    const html = await fetchTextWithRetry(url);
    const $ = load(html);

    if (firstPage) {
      const parsedTotal = parsePageInfo($);
      totalRecords = parsedTotal ?? 0;
      totalPages = Math.max(1, Math.ceil(totalRecords / countRecord));
      firstPage = false;
    }

    const lotRows = parseLotRows($);
    parsedRows += lotRows.length;
    for (const lot of lotRows) {
      rowsCsv += toCsvLine([
        lot.lotNumber,
        code,
        productName,
        lot.announceName,
        lot.lotDesc,
        lot.quantity,
        lot.amount,
        lot.method,
        lot.status,
      ]);
    }

    page += 1;
  }

  return {
    rowsCsv,
    parsedRows,
    totalRecords,
    totalPages,
    truncated: totalRecords >= 10000,
  };
}

async function main() {
  const year = Number(getArgValue('year', '2025'));
  const countRecord = Number(getArgValue('count-record', '2000'));
  const concurrency = Number(getArgValue('concurrency', '4'));
  const limitCodesArg = getArgValue('limit-codes', '');
  const outputPath = getArgValue('output', path.join(process.cwd(), `lots_${year}.csv`));
  const summaryPath = getArgValue('summary', path.join(process.cwd(), `lots_${year}_summary.json`));

  if (!Number.isFinite(year) || !Number.isFinite(countRecord) || !Number.isFinite(concurrency)) {
    throw new Error('year/count-record/concurrency must be numeric');
  }

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });

  console.log('Loading source TRU codes...');
  const sourceCsv = await fetchTextWithRetry(SOURCE_CSV_URL);
  let codes = parseSourceRows(sourceCsv);
  if (limitCodesArg) {
    const limitCodes = Number(limitCodesArg);
    if (Number.isFinite(limitCodes) && limitCodes > 0) {
      codes = codes.slice(0, limitCodes);
    }
  }
  console.log(`Loaded ${codes.length} unique TRU codes.`);

  const out = fs.createWriteStream(outputPath, { flags: 'w' });
  out.write(`${toCsvLine(OUTPUT_HEADERS)}`);

  let writeQueue = Promise.resolve();
  function queueWrite(chunk) {
    writeQueue = writeQueue.then(
      () =>
        new Promise((resolve) => {
          if (!chunk) {
            resolve();
            return;
          }
          if (!out.write(chunk)) {
            out.once('drain', resolve);
            return;
          }
          resolve();
        }),
    );
    return writeQueue;
  }

  let nextIndex = 0;
  let processedCodes = 0;
  let totalRows = 0;
  const truncatedCodes = [];
  const failedCodes = [];

  async function worker(workerId) {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= codes.length) break;

      const item = codes[index];
      const startedAt = Date.now();
      try {
        const result = await collectByCode({
          code: item.code,
          productName: item.productName,
          year,
          countRecord,
        });
        totalRows += result.parsedRows;
        if (result.truncated) truncatedCodes.push(item.code);
        await queueWrite(result.rowsCsv);

        processedCodes += 1;
        const elapsedSec = ((Date.now() - startedAt) / 1000).toFixed(1);
        console.log(
          `[W${workerId}] ${processedCodes}/${codes.length} ${item.code}: ${result.parsedRows} rows, total=${result.totalRecords}, pages=${result.totalPages}, ${elapsedSec}s`,
        );
      } catch (error) {
        processedCodes += 1;
        failedCodes.push({
          code: item.code,
          productName: item.productName,
          error: String(error.message || error),
        });
        console.log(`[W${workerId}] ${processedCodes}/${codes.length} ${item.code}: ERROR ${String(error.message || error)}`);
      }
    }
  }

  const workers = [];
  const workerCount = Math.max(1, concurrency);
  for (let i = 0; i < workerCount; i += 1) {
    workers.push(worker(i + 1));
  }

  await Promise.all(workers);
  await writeQueue;

  out.end();
  await new Promise((resolve) => out.on('finish', resolve));

  const summary = {
    year,
    countRecord,
    concurrency: workerCount,
    totalCodes: codes.length,
    totalRows,
    truncatedCodesCount: truncatedCodes.length,
    truncatedCodes,
    failedCodesCount: failedCodes.length,
    failedCodes,
    outputPath,
    completedAt: new Date().toISOString(),
  };
  fs.writeFileSync(summaryPath, JSON.stringify(summary, null, 2), 'utf8');

  console.log('Done.');
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
