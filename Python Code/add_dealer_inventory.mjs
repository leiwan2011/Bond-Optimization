import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputPath = path.resolve("Input/XBB_holdings.csv");
const outputDir = path.resolve("Output");
const outputCsvPath = path.join(outputDir, "XBB_holdings_with_dealer_inventory.csv");
const outputXlsxPath = path.join(outputDir, "XBB_holdings_with_dealer_inventory.xlsx");

const SEED = 20260613;
const ISSUED_AMOUNT_SEED = 20260614;

function createRng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0;
    return state / 0x100000000;
  };
}

const inventoryRng = createRng(SEED);
const issuedAmountRng = createRng(ISSUED_AMOUNT_SEED);

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];

    if (inQuotes) {
      if (char === '"' && next === '"') {
        field += '"';
        i += 1;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") {
      field += char;
    }
  }

  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }

  return rows;
}

function formatCsvValue(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) {
    return `"${text.replaceAll('"', '""')}"`;
  }
  return text;
}

function toCsv(rows) {
  return rows.map((row) => row.map(formatCsvValue).join(",")).join("\n") + "\n";
}

function numberFromCell(value) {
  const parsed = Number(String(value ?? "").replaceAll(",", ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function sectorGroup(sector) {
  const normalized = String(sector ?? "").trim().toLowerCase();
  if (normalized === "federal") return "federal";
  if (normalized === "provincial" || normalized === "municipal") return "gov";
  return "corporate";
}

function durationBucket(duration) {
  if (duration >= 0 && duration < 5) return "0-5";
  if (duration >= 5 && duration < 10) return "5-10";
  if (duration >= 10 && duration < 14) return "10-14";
  if (duration >= 14 && duration <= 30) return "14-30";
  return null;
}

function factorRange(group) {
  if (group === "federal") return [2.0, 3.0];
  if (group === "gov") return [0.5, 1.5];
  return [0.2, 1.2];
}

function issuedAmount(group) {
  if (group === "federal" || group === "gov") {
    return 500 + Math.floor(issuedAmountRng() * 1501);
  }
  return 100 + Math.floor(issuedAmountRng() * 401);
}

function shuffle(values) {
  const copy = [...values];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(inventoryRng() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

function roundLot(value) {
  return Math.max(1000, Math.round(value / 1000) * 1000);
}

const rawText = await fs.readFile(inputPath, "utf8");
const rows = parseCsv(rawText.replace(/^\uFEFF/, ""));
const headerRowIndex = rows.findIndex((row) => row.includes("Ticker") && row.includes("Shares"));

if (headerRowIndex === -1) {
  throw new Error("Could not find the holdings header row.");
}

const header = rows[headerRowIndex];
const columnIndex = Object.fromEntries(header.map((name, index) => [name, index]));
for (const required of ["Sector", "Duration", "Shares", "Weight (%)"]) {
  if (!(required in columnIndex)) {
    throw new Error(`Missing required column: ${required}`);
  }
}

const dataStart = headerRowIndex + 1;
const groups = new Map();
const inventoryByRow = new Map();
const issuedAmountByRow = new Map();

for (let rowIndex = dataStart; rowIndex < rows.length; rowIndex += 1) {
  const row = rows[rowIndex];
  if (!row.length || row.every((cell) => String(cell).trim() === "")) continue;

  const group = sectorGroup(row[columnIndex.Sector]);
  issuedAmountByRow.set(rowIndex, issuedAmount(group));

  const duration = numberFromCell(row[columnIndex.Duration]);
  const shares = numberFromCell(row[columnIndex.Shares]);
  if (duration === null || shares === null || shares <= 0) continue;

  const bucket = durationBucket(duration);
  if (!bucket) continue;

  const key = `${group}|${bucket}`;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push({ rowIndex, shares, group, bucket });
}

const groupStats = [];
for (const [key, securities] of [...groups.entries()].sort()) {
  const selectedCount = Math.min(securities.length, 5 + Math.floor(inventoryRng() * 6));
  const selected = shuffle(securities).slice(0, selectedCount);
  const [low, high] = factorRange(selected[0].group);

  for (const security of selected) {
    const factor = low + inventoryRng() * (high - low);
    inventoryByRow.set(security.rowIndex, roundLot(security.shares * factor));
  }

  groupStats.push({ key, available: securities.length, selected: selected.length });
}

function addWeightColumns(row) {
  const output = [];
  for (let index = 0; index < header.length; index += 1) {
    if (index === columnIndex["Weight (%)"]) {
      output.push(row[index], row[index], row[index]);
    } else {
      output.push(row[index]);
    }
  }
  return output;
}

const outputHeader = [];
for (let index = 0; index < header.length; index += 1) {
  if (index === columnIndex["Weight (%)"]) {
    outputHeader.push("Pretrade Weight", "Posttrade Weight", "Bmk Weight");
  } else {
    outputHeader.push(header[index]);
  }
}

const outputRows = [
  [...outputHeader, "Dealer Inventory", "Issued Amount"],
  ...rows.slice(dataStart)
    .map((row, offset) => ({ row, rowIndex: dataStart + offset }))
    .filter(({ row }) => row.length && row.some((cell) => String(cell).trim() !== ""))
    .map(({ row, rowIndex }) => [
      ...addWeightColumns(row),
      inventoryByRow.get(rowIndex) ?? 0,
      issuedAmountByRow.get(rowIndex) ?? "",
    ]),
];

await fs.mkdir(outputDir, { recursive: true });
await fs.writeFile(outputCsvPath, toCsv(outputRows), "utf8");

const csvWorkbook = await Workbook.fromCSV(toCsv(outputRows), { sheetName: "XBB Holdings" });
const output = await SpreadsheetFile.exportXlsx(csvWorkbook);
await output.save(outputXlsxPath);

console.log(JSON.stringify({
  outputCsvPath,
  outputXlsxPath,
  selectedRows: inventoryByRow.size,
  groupStats,
}, null, 2));
