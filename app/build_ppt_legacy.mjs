// Legacy MMF-002 renderer retained for regression reference. Not the formal PPT entry.
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const runtimeNodeModules = process.env.RUNTIME_NODE_MODULES;
if (!runtimeNodeModules) throw new Error("RUNTIME_NODE_MODULES is required. Run install.ps1 first.");
const require = createRequire(import.meta.url);
const artifactEntry = require.resolve("@oai/artifact-tool", { paths: [runtimeNodeModules] });
const { Presentation, PresentationFile } = await import(pathToFileURL(artifactEntry).href);

const [inputPath, outputPath, qaDir] = process.argv.slice(2);
if (!inputPath || !outputPath || !qaDir) throw new Error("usage: build_ppt.mjs input.json output.pptx qa_dir");

const C = { navy: "#17365D", blue: "#2E75B6", teal: "#2F7F79", ink: "#1E2A38", muted: "#5E6B78", line: "#D6DEE8", pale: "#F3F6F9", paleBlue: "#EAF2F8", white: "#FFFFFF" };
const FONT = "Microsoft YaHei";

async function writeBlob(filePath, blob) {
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

function addText(slide, name, text, position, style = {}) {
  const shape = slide.shapes.add({ geometry: "textbox", name, position, fill: "none", line: { style: "solid", fill: "none", width: 0 } });
  shape.text = String(text ?? "");
  shape.text.style = { fontFamily: FONT, fontSize: 18, color: C.ink, alignment: "left", verticalAlignment: "middle", ...style };
  return shape;
}

function addChrome(slide, title, core, pageNo) {
  slide.background.fill = C.white;
  slide.shapes.add({ geometry: "rect", name: `top-accent-${pageNo}`, position: { left: 0, top: 0, width: 1280, height: 10 }, fill: C.blue, line: { style: "solid", fill: C.blue, width: 0 } });
  addText(slide, `title-${pageNo}`, title, { left: 72, top: 40, width: 1040, height: 58 }, { fontSize: 38, bold: true, color: C.navy });
  addText(slide, `page-${pageNo}`, String(pageNo).padStart(2, "0"), { left: 1140, top: 48, width: 68, height: 34 }, { fontSize: 17, bold: true, color: C.blue, alignment: "right" });
  slide.shapes.add({ geometry: "line", name: `rule-${pageNo}`, position: { left: 72, top: 108, width: 1136, height: 0 }, fill: "none", line: { style: "solid", fill: C.line, width: 1 } });
  if (core) addText(slide, `core-${pageNo}`, core, { left: 72, top: 120, width: 1136, height: 48 }, { fontSize: 21, bold: true, color: C.teal });
}

function renderOverview(slide, data, index) {
  addChrome(slide, data.title, data.core_message, index);
  const bullets = (data.bullets || []).slice(0, 5);
  bullets.forEach((text, i) => {
    addText(slide, `overview-num-${index}-${i}`, String(i + 1).padStart(2, "0"), { left: 88, top: 208 + i * 82, width: 48, height: 30 }, { fontSize: 15, bold: true, color: C.blue });
    addText(slide, `overview-bullet-${index}-${i}`, text, { left: 150, top: 192 + i * 82, width: 980, height: 64 }, { fontSize: 22, color: C.ink });
    if (i < bullets.length - 1) slide.shapes.add({ geometry: "line", name: `overview-line-${index}-${i}`, position: { left: 150, top: 267 + i * 82, width: 980, height: 0 }, fill: "none", line: { style: "solid", fill: C.line, width: 1 } });
  });
}

function renderTable(slide, data, index) {
  addChrome(slide, data.title, data.core_message, index);
  const tableData = data.table || {};
  const columns = tableData.columns || [];
  const rows = tableData.rows || [];
  if (!columns.length || !rows.length) return renderOverview(slide, data, index);
  const values = [columns, ...rows.slice(0, 6)];
  const tracks = columns.map((_, i) => ({ mode: "fr", value: i === 0 ? 1.1 : 2.0 }));
  const table = slide.tables.add({ rows: values.length, columns: columns.length, left: 72, top: 192, width: 1136, height: 430, columnTracks: tracks, values });
  table.borders.assign({ style: "solid", fill: C.line, width: 1 });
  table.styleOptions = { headerRow: true, bandedRows: false };
  for (let r = 0; r < values.length; r++) for (let c = 0; c < columns.length; c++) {
    const cell = table.getCell(r, c);
    cell.fill = r === 0 ? C.navy : (r % 2 ? C.pale : C.white);
    cell.text.style = { fontFamily: FONT, fontSize: r === 0 ? 18 : 16, bold: r === 0 || c === 0, color: r === 0 ? C.white : C.ink, alignment: c === 0 ? "center" : "left", verticalAlignment: "middle" };
  }
}

function renderProcess(slide, data, index) {
  addChrome(slide, data.title, data.core_message, index);
  const steps = (data.steps || []).slice(0, 5);
  if (!steps.length) return renderOverview(slide, data, index);
  const gap = 205;
  const xs = steps.map((_, i) => 95 + i * gap);
  const nodes = steps.map((step, i) => slide.shapes.add({ geometry: "ellipse", name: `node-${index}-${i}`, position: { left: xs[i], top: 228, width: 56, height: 56 }, fill: i === steps.length - 1 ? C.teal : C.blue, line: { style: "solid", fill: C.white, width: 2 } }));
  for (let i = 0; i < nodes.length - 1; i++) slide.shapes.connect(nodes[i], nodes[i + 1], { kind: "straight", fromSide: "right", toSide: "left", line: { style: "solid", fill: C.line, width: 4 }, tail: { type: "triangle", width: "sm", length: "sm" } });
  steps.forEach((step, i) => {
    nodes[i].text = String(i + 1);
    nodes[i].text.style = { fontFamily: FONT, fontSize: 20, bold: true, color: C.white, alignment: "center", verticalAlignment: "middle" };
    nodes[i].bringToFront();
    addText(slide, `step-title-${index}-${i}`, step.title, { left: xs[i] - 45, top: 306, width: 146, height: 42 }, { fontSize: 23, bold: true, color: C.navy, alignment: "center" });
    addText(slide, `step-body-${index}-${i}`, step.body, { left: xs[i] - 64, top: 360, width: 184, height: 205 }, { fontSize: 16, color: C.ink, verticalAlignment: "top" });
  });
}

function renderModules(slide, data, index) {
  addChrome(slide, data.title, data.core_message, index);
  const modules = (data.modules || []).slice(0, 4);
  if (!modules.length) return renderOverview(slide, data, index);
  slide.shapes.add({ geometry: "line", name: `v-${index}`, position: { left: 640, top: 205, width: 0, height: 395 }, fill: "none", line: { style: "solid", fill: C.line, width: 1.5 } });
  slide.shapes.add({ geometry: "line", name: `h-${index}`, position: { left: 72, top: 405, width: 1136, height: 0 }, fill: "none", line: { style: "solid", fill: C.line, width: 1.5 } });
  const positions = [{ left: 72, top: 205 }, { left: 688, top: 205 }, { left: 72, top: 435 }, { left: 688, top: 435 }];
  modules.forEach((mod, i) => {
    const p = positions[i];
    addText(slide, `mod-num-${index}-${i}`, String(i + 1).padStart(2, "0"), { left: p.left, top: p.top, width: 48, height: 32 }, { fontSize: 15, bold: true, color: C.blue });
    addText(slide, `mod-title-${index}-${i}`, mod.title, { left: p.left + 62, top: p.top - 3, width: 448, height: 42 }, { fontSize: 25, bold: true, color: C.navy });
    addText(slide, `mod-body-${index}-${i}`, mod.body, { left: p.left + 62, top: p.top + 48, width: 440, height: 118 }, { fontSize: 16, color: C.ink, verticalAlignment: "top" });
  });
}

async function main() {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(qaDir, { recursive: true });
  const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
  const brief = payload.brief;
  const artifact = payload.artifact;
  const deck = Presentation.create({ slideSize: { width: 1280, height: 720 } });

  const cover = deck.slides.add();
  cover.background.fill = C.white;
  cover.shapes.add({ geometry: "rect", name: "cover-accent", position: { left: 0, top: 0, width: 1280, height: 18 }, fill: C.blue, line: { style: "solid", fill: C.blue, width: 0 } });
  addText(cover, "cover-project", brief.project_name, { left: 80, top: 86, width: 920, height: 36 }, { fontSize: 20, bold: true, color: C.blue });
  addText(cover, "cover-title", artifact.title, { left: 80, top: 160, width: 1000, height: 150 }, { fontSize: 54, bold: true, color: C.navy, verticalAlignment: "top" });
  addText(cover, "cover-subtitle", artifact.subtitle || `${brief.project_name}｜${brief.scenario}`, { left: 80, top: 340, width: 840, height: 72 }, { fontSize: 24, color: C.muted });
  addText(cover, "cover-medium", "物业方案章节初稿", { left: 80, top: 610, width: 350, height: 32 }, { fontSize: 17, bold: true, color: C.teal });

  for (const [i, data] of (artifact.slides || []).entries()) {
    const slide = deck.slides.add();
    const layout = data.layout || "overview";
    if (layout === "table" || layout === "responsibility_matrix" || layout === "comparison") renderTable(slide, data, i + 2);
    else if (layout === "process" || layout === "timeline") renderProcess(slide, data, i + 2);
    else if (layout === "modules") renderModules(slide, data, i + 2);
    else renderOverview(slide, data, i + 2);
  }

  for (const [index, slide] of deck.slides.items.entries()) {
    const stem = `slide-${String(index + 1).padStart(2, "0")}`;
    await writeBlob(`${qaDir}/${stem}.png`, await deck.export({ slide, format: "png", scale: 1.5 }));
    await fs.writeFile(`${qaDir}/${stem}.layout.json`, await (await slide.export({ format: "layout" })).text());
  }
  await writeBlob(`${qaDir}/deck-montage.webp`, await deck.export({ format: "webp", montage: true, scale: 1 }));
  const pptx = await PresentationFile.exportPptx(deck);
  await pptx.save(outputPath);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
