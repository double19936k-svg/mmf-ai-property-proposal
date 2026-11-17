import fs from "node:fs/promises";
import path from "node:path";
import {execFileSync} from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { W, H, configureTheme } from "./ppt_renderer_v2/theme.mjs";
import { routeSlide } from "./ppt_renderer_v2/router.mjs";
import { renderVisualScene } from "./ppt_renderer_v2/visuals.mjs";
import { renderCover, renderDivider, renderClosing, renderModules, renderProcess, renderTable, renderOverview, renderServiceSlide, getImageManifest } from "./ppt_renderer_v2/components.mjs";
import { projectEvidence } from "./ppt_renderer_v2/service_evidence.mjs";

import { modulePath } from "./windows_paths.mjs";
const runtimeNodeModules = modulePath(process.env.RUNTIME_NODE_MODULES || "");
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

async function toDataUrl(filePath) {
  if (!filePath) return "";
  try {
    const blob = await fs.readFile(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const mime = ext === ".png" ? "image/png" : "image/jpeg";
    return `data:${mime};base64,${blob.toString("base64")}`;
  } catch {
    return "";
  }
}

async function preloadImages(payload) {
  // Profile-P embeds verified bytes in components; legacy path behavior stays compatible.
}

async function main() {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(qaDir, { recursive: true });
  const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
  await preloadImages(payload);
  const brief = payload.brief || {};
  const artifact = payload.artifact || {};
  const serviceProfile = artifact.profile_p_contract === "profile-p-service-v1";
  const evidence = serviceProfile ? projectEvidence(brief) : null;
  const slides = artifact.slides || [];
  const dividerChapterIds = new Set((artifact.chapters || []).filter((row) => row.chapter_id && (serviceProfile || row.chapter_id !== "P01")).map((row) => row.chapter_id));
  const dividerTotal = new Set(slides.map((s) => s.chapter_id).filter((cid) => dividerChapterIds.has(cid))).size;
  const totalPages = slides.length + 1 + dividerTotal + (serviceProfile ? 2 : 0);
  const runId = runIdFromInput(inputPath);
  const deck = Presentation.create({ slideSize: { width: W, height: H } });
  configureTheme(deck);

  const cover = deck.slides.add();
  renderCover(cover, brief, artifact, totalPages, runId);

  const routingAudit = [];
  if (serviceProfile) {
    const navigation = deck.slides.add();
    renderServiceSlide(navigation, { title: "服务范围与阅读导航", core_message: "按项目条件与服务阶段阅读本方案",
      _navigation: artifact.chapters || [], _evidence: evidence,
      profile_p_contract: artifact.profile_p_contract, _project_name: brief.project_name }, 2, totalPages,
      {component_id: "PPT-SERVICE-NAVIGATION"}, runId);
    routingAudit.push({slide_no: 2, title: "服务范围与阅读导航", semantic_layout: "navigation"});
  }
  const images = artifact.images || {};
  const chapters = (artifact.chapters || []).filter((row) => row.chapter_id && (serviceProfile || row.chapter_id !== "P01"));
  const dividerByChapter = new Map(chapters.map((row) => [row.chapter_id, row]));
  let dividerCount = 0;
  let hasEvidenceCharts = false;
  const seenChapters = new Set();
  let pageNo = serviceProfile ? 2 : 1;
  for (const data of slides) {
    const cid = data.chapter_id || "";
    if (cid && dividerByChapter.has(cid) && !seenChapters.has(cid)) {
      seenChapters.add(cid);
      dividerCount += 1;
      pageNo += 1;
      const chapter = dividerByChapter.get(cid);
      const dividerData = {
        no: String(dividerCount + (serviceProfile ? 0 : 1)).padStart(2, "0"),
        title: chapter.title,
        goal: chapter.goal,
        service: serviceProfile,
        topics: slides.filter((row) => row.chapter_id === cid).slice(0, 4).map((row) => row.title).filter(Boolean),
        image: serviceProfile ? "" : images.by_chapter?.[cid] || "",
      };
      const dividerSlide = deck.slides.add();
      renderDivider(dividerSlide, dividerData, pageNo, totalPages, runId);
      routingAudit.push({ slide_no: pageNo, title: `章节隔页：${chapter.title}`, semantic_layout: "divider", component_id: "MMF-DIVIDER-THEME-D", reuse_basis: "Theme D frozen", content_semantics_changed: false });
    }
    const route = routeSlide(data);
    const renderData = {
      ...data,
      _scenario: brief.scenario,
      _project_name: brief.project_name,
      _evidence: evidence,
      _image: data.image || (serviceProfile ? "" : images.by_chapter?.[data.chapter_id] || ""),
    };
    const slide = deck.slides.add();
    pageNo += 1;
    if (serviceProfile && data.topic_id === "project_overview" && data.layout === "modules" &&
        !renderData._image && evidence.charts.length) hasEvidenceCharts = true;
    if (serviceProfile) renderServiceSlide(slide, renderData, pageNo, totalPages, route, runId);
    else if (data.layout === "visual") renderVisualScene(slide, renderData, pageNo, totalPages);
    else if (route.component_id.startsWith("SK-06")) renderModules(slide, renderData, pageNo, totalPages, route, runId);
    else if (route.component_id.startsWith("SK-07") || route.component_id.startsWith("SK-08")) renderProcess(slide, renderData, pageNo, totalPages, route, runId);
    else if (route.component_id.startsWith("SK-13")) renderTable(slide, renderData, pageNo, totalPages, route, runId);
    else renderOverview(slide, renderData, pageNo, totalPages, route, runId);
    routingAudit.push({ slide_no: pageNo, title: data.title, slide_id: data.slide_id, ...route,
      ...(data.layout === "visual" ? {component_id:"MMF-VISUAL-IR-V1", semantic_digest:data.visual_scene.semantic_digest} : {}),
      content_semantics_changed: false });
  }

  const closing = deck.slides.add();
  renderClosing(closing, brief, artifact, totalPages, runId);

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
  await fs.writeFile(path.join(qaDir, "image_manifest.json"), JSON.stringify(getImageManifest(), null, 2), "utf8");
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
  if (hasEvidenceCharts) {
    const rawPath = path.join(qaDir, "native-charts-raw.pptx");
    const editablePath = path.join(qaDir, "native-charts-editable.pptx");
    try {
      await pptx.save(rawPath);
      execFileSync(process.env.MMF_PPT_PYTHON || (process.platform === "win32" ? "python" : "python3"),
        [fileURLToPath(new URL("./ppt_chart_data.py", import.meta.url)), rawPath, editablePath],
        {stdio:"inherit",windowsHide:true,timeout:30_000});
      await fs.copyFile(editablePath, outputPath);
    } finally {
      await fs.rm(rawPath, {force:true});
      await fs.rm(editablePath, {force:true});
    }
  } else await pptx.save(outputPath);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
