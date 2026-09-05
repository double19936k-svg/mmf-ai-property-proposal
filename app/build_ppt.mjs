import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { W, H, configureTheme } from "./ppt_renderer_v2/theme.mjs";
import { routeSlide } from "./ppt_renderer_v2/router.mjs";
import { renderCover, renderModules, renderProcess, renderTable, renderOverview } from "./ppt_renderer_v2/components.mjs";

const runtimeNodeModules = process.env.RUNTIME_NODE_MODULES;
if (!runtimeNodeModules) throw new Error("RUNTIME_NODE_MODULES is required. Run install.ps1 first.");
const require = createRequire(import.meta.url);
const artifactEntry = require.resolve("@oai/artifact-tool", { paths: [runtimeNodeModules] });
const { Presentation, PresentationFile } = await import(pathToFileURL(artifactEntry).href);

const [inputPath, outputPath, qaDir] = process.argv.slice(2);
if (!inputPath || !outputPath || !qaDir) throw new Error("usage: build_ppt.mjs input.json output.pptx qa_dir");

async function writeBlob(filePath, blob) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

function runIdFromInput(filePath) {
  return path.basename(path.dirname(filePath));
}

async function main() {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(qaDir, { recursive: true });
  const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
  const brief = payload.brief || {};
  const artifact = payload.artifact || {};
  const slides = artifact.slides || [];
  const totalPages = slides.length + 1;
  const runId = runIdFromInput(inputPath);
  const deck = Presentation.create({ slideSize: { width: W, height: H } });
  configureTheme(deck);

  const cover = deck.slides.add();
  renderCover(cover, brief, artifact, totalPages, runId);

  const routingAudit = [];
  for (const [index, data] of slides.entries()) {
    const route = routeSlide(data);
    const renderData = { ...data, _scenario: brief.scenario, _project_name: brief.project_name };
    const slide = deck.slides.add();
    const pageNo = index + 2;
    if (route.component_id.startsWith("SK-06")) renderModules(slide, renderData, pageNo, totalPages, route, runId);
    else if (route.component_id.startsWith("SK-07") || route.component_id.startsWith("SK-08")) renderProcess(slide, renderData, pageNo, totalPages, route, runId);
    else if (route.component_id.startsWith("SK-13")) renderTable(slide, renderData, pageNo, totalPages, route, runId);
    else renderOverview(slide, renderData, pageNo, totalPages, route, runId);
    routingAudit.push({ slide_no: pageNo, title: data.title, ...route, content_semantics_changed: false });
  }

  const previewIndex = [];
  for (const [index, slide] of deck.slides.items.entries()) {
    const stem = `slide-${String(index + 1).padStart(2, "0")}`;
    const pngPath = path.join(qaDir, `${stem}.png`);
    const layoutPath = path.join(qaDir, `${stem}.layout.json`);
    await writeBlob(pngPath, await deck.export({ slide, format: "png", scale: 1.5 }));
    await fs.writeFile(layoutPath, await (await slide.export({ format: "layout" })).text(), "utf8");
    previewIndex.push({ slide_no: index + 1, png: pngPath, layout: layoutPath });
  }
  await writeBlob(path.join(qaDir, "deck-montage.webp"), await deck.export({ format: "webp", montage: true, scale: 1 }));
  const inspect = await deck.inspect({ kind: "slide,textbox,shape,table,notes,layout", maxChars: 200000 });
  await fs.writeFile(`${outputPath}.inspect.ndjson`, inspect.ndjson, "utf8");
  await fs.writeFile(path.join(qaDir, "component_routing_audit.json"), JSON.stringify(routingAudit, null, 2), "utf8");
  await fs.writeFile(path.join(qaDir, "preview_index.json"), JSON.stringify(previewIndex, null, 2), "utf8");
  await fs.writeFile(path.join(qaDir, "rendering_integrity.json"), JSON.stringify({
    renderer: "MMF PPT Renderer V2",
    theme: "Theme D V1.1 frozen",
    input: inputPath,
    output: outputPath,
    slide_count: deck.slides.items.length,
    content_semantics_changed: false,
    ai_called: false
  }, null, 2), "utf8");
  const pptx = await PresentationFile.exportPptx(deck);
  await pptx.save(outputPath);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
