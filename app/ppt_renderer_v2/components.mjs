import { THEME, W, H } from "./theme.mjs";
import { readFileSync } from "node:fs";
import { layouts as serviceLayouts } from "./service_evidence.mjs";

const c = THEME.colors;
const f = THEME.fonts;

function shape(slide, geometry, name, position, fill = "none", stroke = "none", width = 0) {
  return slide.shapes.add({ geometry, name, position, fill, line: { style: "solid", fill: stroke, width } });
}

function rect(slide, name, left, top, width, height, fill, stroke = fill, lineWidth = 0) {
  return shape(slide, "rect", name, { left, top, width, height }, fill, stroke, lineWidth);
}

function round(slide, name, left, top, width, height, fill, stroke, lineWidth = 1) {
  const item = shape(slide, "roundRect", name, { left, top, width, height }, fill, stroke, lineWidth);
  item.borderRadius = "rounded-md";
  return item;
}

function line(slide, name, left, top, width, height, color, lineWidth = 1) {
  return shape(slide, "line", name, { left, top, width: Math.max(1, width), height: Math.max(1, height) }, "none", color, lineWidth);
}

const TONE_PAIRS = JSON.parse(readFileSync(new URL("./service_tone.json", import.meta.url), "utf8"));

function pptTone(value) {
  let textValue = String(value ?? "");
  for (const [from, to] of TONE_PAIRS) textValue = textValue.split(from).join(to);
  return textValue;
}

function wrapCoverTitle(value, keepSpaces=false) {
  const raw = String(value || "").trim();
  const title = keepSpaces ? raw : raw.replace(/\s+/g, "");
  if ([...title].length <= 8) return title;
  const tail = title.match(/^(.*?)(产业园|写字楼|综合体|广场|大厦)$/);
  if (tail && [...tail[1]].length >= 4 && [...tail[2]].length >= 2) return `${tail[1]}\n${tail[2]}`;
  return title;
}

function text(slide, name, value, left, top, width, height, size, color, bold = false, alignment = "left", fontFamily = f.body, verticalAlignment = "middle") {
  const item = shape(slide, "textbox", name, { left, top, width, height });
  item.text = String(value ?? "");
  item.text.style = { fontSize: size, color, bold, alignment, verticalAlignment, margin: 0, wrap: true, fontFamily, typeface: fontFamily };
  return item;
}


const imageManifest = [];
function nativeImageSize(filePath) {
  let buf;
  try { buf = readFileSync(filePath); } catch { return null; }
  if (buf.length < 24) return null;
  if (buf[0] === 0x89 && buf.toString("latin1", 1, 4) === "PNG") return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  if (buf[0] === 0xff && buf[1] === 0xd8) {
    let offset = 2;
    while (offset + 9 < buf.length) {
      if (buf[offset] !== 0xff) { offset++; continue; }
      const marker = buf[offset + 1];
      if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
        return { height: buf.readUInt16BE(offset + 5), width: buf.readUInt16BE(offset + 7) };
      }
      offset += 2 + buf.readUInt16BE(offset + 2);
    }
    return null;
  }
  if (buf.toString("latin1", 0, 6).startsWith("GIF8")) return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) };
  if (buf.toString("latin1", 0, 4) === "RIFF" && buf.toString("latin1", 8, 12) === "WEBP") {
    const format = buf.toString("latin1", 12, 16);
    if (format === "VP8X") return { width: 1 + buf.readUIntLE(24, 3), height: 1 + buf.readUIntLE(27, 3) };
    if (format === "VP8 ") return { width: buf.readUInt16LE(26) & 0x3fff, height: buf.readUInt16LE(28) & 0x3fff };
    if (format === "VP8L") { const bits = buf.readUInt32LE(21); return { width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1 }; }
  }
  return null;
}
function fitInside(sourcePath, position) {
  const size = nativeImageSize(sourcePath);
  if (!size || !size.width || !size.height || !position?.width || !position?.height) return position;
  const scale = Math.min(position.width / size.width, position.height / size.height);
  const width = size.width * scale;
  const height = size.height * scale;
  return { ...position, left: position.left + (position.width - width) / 2, top: position.top + (position.height - height) / 2, width, height };
}
function addImageManaged(slide, sourcePath, name, position, alt, embedBytes = false) {
  if (!sourcePath) return null;
  const source = embedBytes ? {blob: new Uint8Array(readFileSync(sourcePath)),
    contentType: /\.jpe?g$/i.test(sourcePath) ? "image/jpeg" : "image/png"} : {path: sourcePath};
  const img = slide.images.add({ ...source, name, position: fitInside(sourcePath, position) });
  img.alt = alt || name;
  imageManifest.push(String(sourcePath));
  return img;
}
export function getImageManifest() {
  return [...imageManifest];
}

function addNotes(slide, componentId, runId) {
  slide.speakerNotes.textFrame.setText([
    `MMF PPT Renderer V2 / ${componentId}`,
    `content_semantics_changed: false`,
    `[Sources]`,
    `- 业务内容：${runId}/generation_raw.json（原文直接映射）`,
    `- 视觉系统：Theme D V1.1与第一阶段已验收/冻结组件模式`
  ]);
  slide.speakerNotes.setVisible(true);
}

function addHeader(slide, data, pageNo, totalPages, componentId, runId) {
  slide.background.fill = c.background;
  rect(slide, `header-accent-${pageNo}`, 60, 28, 4, 62, c.primary);
  text(slide, `kicker-${pageNo}`, "物业服务方案汇报", 76, 20, 550, 22, 13, c.secondary, true, "left", f.subtitle);
  text(slide, `title-${pageNo}`, data.title, 76, 44, 1060, 46, THEME.typography.slideTitle, c.ink, true, "left", f.title);
  line(slide, `header-rule-${pageNo}`, 60, 100, 1160, 1, c.line, 1);
  text(slide, `lead-${pageNo}`, data.core_message || "", 76, 111, 1085, 42, THEME.typography.lead, c.muted, false, "left", f.body);
  text(slide, `page-${pageNo}`, `${String(pageNo).padStart(2, "0")} / ${String(totalPages).padStart(2, "0")}`, 1085, 677, 135, 18, 10, "#819196", false, "right", f.numeric);
  text(slide, `project-${pageNo}`, data._project_name || "", 60, 677, 420, 18, 10, "#819196", false, "left", f.body);
  addNotes(slide, componentId, runId);
}

export function renderDivider(slide, chapter, pageNo, totalPages, runId) {
  slide.background.fill = c.deep;
  const imgPath = chapter.image;
  if (chapter.service) {
    const x = 660;
    rect(slide, "chapter-right", x, 0, W-x, H, c.primary);
    text(slide, "chapter-kicker", "物业服务方案   /   章节导览", 72, 75, 500, 28, 16, c.light, true);
    text(slide, "chapter-number", chapter.no, 72, 142, 520, 132, 106, "#E4BF8C", true);
    text(slide, "chapter-title", chapter.title, 76, 300, 530, 104, 46, c.white, true, "left", f.title);
    line(slide, "chapter-rule", 76, 436, 70, 1, "#E4BF8C", 2);
    text(slide, "chapter-goal", chapter.goal, 76, 464, 512, 116, 23, c.light, false, "left", f.body, "top");
    for (const [i, topic] of (chapter.topics || []).slice(0, 4).entries()) {
      const y = 191 + i * 96;
      line(slide, `chapter-topic-rule-${i}`, 730, y, 460, 1, "#44717B");
      text(slide, `chapter-topic-index-${i}`, String(i+1).padStart(2,"0"), 730, y+15, 48, 52, 18, "#E4BF8C", true);
      text(slide, `chapter-topic-${i}`, pptTone(topic), 790, y+15, 400, 52, 22, c.white);
    }
    text(slide, "chapter-page", `${pageNo} / ${totalPages}`, 1080, 675, 110, 25, 15, c.light, false, "right");
    addNotes(slide, "PPT-SERVICE-CHAPTER", runId);
    return;
  }
  if (imgPath) {
    addImageManaged(slide, imgPath, `divider-photo-${pageNo}`, { left: 680, top: 0, width: W - 680, height: H }, "章节配图");
    rect(slide, `divider-fade-${pageNo}`, 680, 0, 140, H, c.deep);
  }
  rect(slide, `divider-band-${pageNo}`, 664, 0, 16, H, c.gold);
  text(slide, `divider-no-${pageNo}`, chapter.no, 84, 200, 200, 72, 60, c.medium, true, "left", f.numeric);
  text(slide, `divider-title-${pageNo}`, chapter.title, 84, 292, 560, 60, 40, c.white, true, "left", f.title);
  line(slide, `divider-rule-${pageNo}`, 84, 380, 220, 2, c.gold, 2);
  text(slide, `divider-goal-${pageNo}`, chapter.goal, 84, 404, 540, 90, 17, c.light, false, "left", f.body, "top");
  const topics = imgPath ? [] : (chapter.topics || []).slice(0, 4);
  topics.forEach((topic, index) => {
    const y = 150 + index * 118;
    round(slide, `divider-card-${pageNo}-${index}`, 760, y, 440, 100, "#164854", "#2A6573", 1);
    rect(slide, `divider-card-bar-${pageNo}-${index}`, 760, y, 8, 100, c.gold);
    text(slide, `divider-card-text-${pageNo}-${index}`, topic, 788, y + 18, 390, 64, 18, c.white, false, "left", f.body);
  });
  text(slide, `divider-page-${pageNo}`, `${String(pageNo).padStart(2, "0")} / ${String(totalPages).padStart(2, "0")}`, 1085, 677, 135, 18, 10, c.medium, false, "right", f.numeric);
  addNotes(slide, "MMF-DIVIDER-THEME-D", runId);
}

export function renderCover(slide, brief, artifact, totalPages, runId) {
  slide.background.fill = c.deep;
  if (artifact.profile_p_contract === "profile-p-service-v1") {
    const imagePath = artifact.images?.cover;
    const title = wrapCoverTitle(brief.project_name || artifact.title,true);
    rect(slide, "service-cover-aside", 632, 0, 648, H, c.primary);
    if (imagePath) {
      rect(slide, "service-cover-image-edge", 654, 80, 588, 560, "none", "#44717B", 1);
      addImageManaged(slide, imagePath, "service-cover-photo", {left: 671, top: 96, width: 554, height: 528},
        artifact.images?.cover_caption || "项目资料图片", true);
    } else {
      text(slide, "service-cover-type", brief.project_type || "物业服务", 677, 200, 520, 240, 57, c.light, true);
    }
    text(slide, "service-cover-kicker", "物业服务方案  /  项目汇报", 70, 91, 510, 26, 18, "#E4BF8C", true);
    line(slide, "service-cover-rule", 70, 149, 74, 1, "#E4BF8C", 2);
    const longest = Math.max(...title.split("\n").map(line => weightedLength(line)), 1);
    text(slide, "service-cover-title", title, 70, 173, 516, 256, Math.max(28, Math.min(61, Math.floor(500 / longest))),
      c.white, true, "left", f.title, "middle");
    text(slide, "service-cover-subtitle", brief.scenario || artifact.subtitle || "物业服务方案汇报", 70, 465, 510, 76, 24, c.light);
    if (brief.location) text(slide, "service-cover-location", brief.location, 70, 603, 510, 40, 17, c.light);
    addNotes(slide, "PPT-SERVICE-COVER", runId);
    return;
  }
  // 右侧图片面板（若无配图则用主题色面板），左侧深色区只承载 标题+副标题。
  const imgPath = artifact?.images?.cover;
  if (imgPath) {
    addImageManaged(slide, imgPath, "cover-photo", { left: 560, top: 0, width: W - 560, height: H }, "封面配图", Boolean(artifact.profile_p_contract));
    rect(slide, "cover-photo-fade", 560, 0, 120, H, c.deep);
  } else {
    rect(slide, "cover-panel", 560, 0, W - 560, H, c.primary);
  }
  rect(slide, "cover-band", 548, 0, 12, H, c.gold);
  text(slide, "cover-kicker", "PROPERTY SERVICE PROPOSAL", 84, 96, 460, 26, 13, c.secondary, true, "left", f.subtitle);
  const coverTitle = wrapCoverTitle(artifact.title);
  text(slide, "cover-title", coverTitle, 84, 160, 500, 250, coverTitle.includes("\n") ? 40 : THEME.typography.deckTitle, c.white, true, "left", f.title, "top");
  line(slide, "cover-rule", 84, 448, 380, 2, c.gold, 2);
  text(slide, "cover-subtitle", artifact.subtitle || "物业服务方案汇报", 84, 474, 430, 90, 22, c.light, false, "left", f.subtitle, "top");
  text(slide, "cover-project", brief.project_name || "", 84, 600, 430, 30, 15, c.secondary, true, "left", f.body);
  const dateText = new Date().toISOString().slice(0, 10);
  text(slide, "cover-date", dateText, 84, 634, 300, 24, 13, c.medium, false, "left", f.numeric);
  text(slide, "cover-page", `01 / ${String(totalPages).padStart(2, "0")}`, 1085, 677, 135, 18, 10, c.medium, false, "right", f.numeric);
  addNotes(slide, "MMF-COVER-THEME-D", runId);
}

function gridRows(count) {
  if (count <= 3) return [count];
  if (count === 4) return [2, 2];
  if (count === 5) return [3, 2];
  if (count === 6) return [3, 3];
  if (count === 7) return [4, 3];
  return [4, 4];
}

export function renderModules(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const modules = (data.modules || []).slice(0, 8);
  const rows = gridRows(modules.length);
  const region = { left: 60, right: 1220, top: 190, bottom: 660 };
  const rowGap = 22;
  const estLines = (item) => Math.max(2, Math.ceil([...String(item?.body || "")].length / 21));
  const maxLines = Math.max(2, ...modules.map(estLines));
  const rowHeight = rows.length === 1 ? Math.min(300, Math.max(170, 96 + maxLines * 30)) : Math.min(182, Math.max(150, 80 + maxLines * 30));
  const totalHeight = rowHeight * rows.length + rowGap * (rows.length - 1);
  let offset = 0;
  rows.forEach((count, rowIndex) => {
    const gap = count >= 4 ? 18 : 24;
    const maxWidth = count === 3 ? 350 : count === 2 ? 410 : 270;
    const rawWidth = (region.right - region.left - gap * (count - 1)) / count;
    const cardWidth = Math.min(maxWidth, rawWidth);
    const rowWidth = cardWidth * count + gap * (count - 1);
    const startX = region.left + ((region.right - region.left) - rowWidth) / 2;
    const y = region.top + ((region.bottom - region.top) - totalHeight) / 2 + rowIndex * (rowHeight + rowGap);
    for (let i = 0; i < count; i += 1) {
      const item = modules[offset + i];
      const x = startX + i * (cardWidth + gap);
      round(slide, `module-card-${offset + i}`, x, y, cardWidth, rowHeight, c.panel, c.line, 1);
      rect(slide, `module-top-${offset + i}`, x, y, cardWidth, 8, i === 0 ? c.deep : c.primary);
      round(slide, `module-no-bg-${offset + i}`, x + 20, y + 24, 44, 30, i === 0 ? c.deep : c.soft, i === 0 ? c.deep : c.line, 1);
      text(slide, `module-no-${offset + i}`, String(offset + i + 1).padStart(2, "0"), x + 20, y + 29, 44, 18, 11, i === 0 ? c.white : c.primary, true, "center", f.numeric);
      text(slide, `module-title-${offset + i}`, item.title, x + 78, y + 19, cardWidth - 98, 44, 20, c.ink, true, "left", f.body);
      line(slide, `module-rule-${offset + i}`, x + 20, y + 74, cardWidth - 40, 1, c.line, 1);
      text(slide, `module-body-${offset + i}`, item.body, x + 20, y + 88, cardWidth - 40, rowHeight - 112, 16, c.muted, false, "left", f.body, "top");
    }
    offset += count;
  });
}

export function renderProcess(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const steps = (data.steps || []).slice(0, 5);
  const count = steps.length;
  const gap = count <= 3 ? 46 : 24;
  const nodeWidth = count <= 3 ? 338 : count === 4 ? 266 : 210;
  const totalWidth = nodeWidth * count + gap * (count - 1);
  const startX = (W - totalWidth) / 2;
  const top = 214;
  const height = 320;
  for (let i = 0; i < count - 1; i += 1) {
    const arrowX = startX + (i + 1) * nodeWidth + i * gap + 8;
    shape(slide, "rightArrow", `process-arrow-${i}`, { left: arrowX, top: top + 132, width: gap - 16, height: 54 }, c.primary, c.primary, 0);
  }
  steps.forEach((step, index) => {
    const x = startX + index * (nodeWidth + gap);
    round(slide, `process-node-${index}`, x, top, nodeWidth, height, c.panel, index === 0 ? c.deep : c.line, index === 0 ? 2 : 1);
    rect(slide, `process-node-top-${index}`, x, top, nodeWidth, 8, index === 0 ? c.deep : c.primary);
    round(slide, `process-no-bg-${index}`, x + 22, top + 28, 50, 34, index === 0 ? c.deep : c.soft, index === 0 ? c.deep : c.line, 1);
    text(slide, `process-no-${index}`, String(index + 1).padStart(2, "0"), x + 22, top + 34, 50, 20, 12, index === 0 ? c.white : c.primary, true, "center", f.numeric);
    text(slide, `process-title-${index}`, step.title, x + 88, top + 23, nodeWidth - 110, 48, 21, c.ink, true, "left", f.body);
    line(slide, `process-rule-${index}`, x + 22, top + 86, nodeWidth - 44, 1, c.line, 1);
    text(slide, `process-body-${index}`, step.body, x + 22, top + 106, nodeWidth - 44, 190, 16, c.muted, false, "left", f.body, "top");
    rect(slide, `process-node-accent-${index}`, x + 22, top + 298, nodeWidth - 44, 6, index === count - 1 ? c.primary : c.soft, index === count - 1 ? c.primary : c.soft, 0);
  });
}

export function renderTable(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const columns = data.table?.columns || [];
  const rows = (data.table?.rows || []).slice(0, 7);
  if (!columns.length || !rows.length) return renderOverview(slide, data, pageNo, totalPages, route, runId);
  const values = [columns, ...rows];
  // 列宽按各列实际内容最大长度成比例分配，避免固定对半分。
  const maxLen = columns.map((_, col) => Math.max(...values.map((row) => [...String(row[col] || "")].length), 1));
  const totalLen = maxLen.reduce((a, b) => a + b, 0);
  const columnTracks = maxLen.map((len) => ({ mode: "fr", value: Math.max(0.85, Math.min(3.2, (len / totalLen) * columns.length * 1.6)) }));
  const table = slide.tables.add({ rows: values.length, columns: columns.length, left: 60, top: 186, width: 1160, height: 370, columnTracks, values });
  table.styleOptions = { headerRow: true, bandedRows: false };
  table.borders.assign({ style: "solid", fill: c.grid, width: 1 });
  for (let row = 0; row < values.length; row += 1) {
    for (let col = 0; col < columns.length; col += 1) {
      const cell = table.getCell(row, col);
      if (row === 0) cell.fill = c.primary;
      else if (col === 0) cell.fill = row === 1 ? c.primary : c.soft;
      else cell.fill = row % 2 === 0 ? c.pale : c.panel;
      cell.text.style = {
        fontSize: row === 0 ? 16 : 15,
        bold: row === 0 || col === 0,
        color: row === 0 || (col === 0 && row === 1) ? c.white : col === 0 ? c.primary : c.ink,
        fontFamily: f.body,
        alignment: col === 1 ? "left" : "center",
        verticalAlignment: "middle",
        margin: 5,
        wrap: true
      };
    }
  }
}

export function renderClosing(slide, brief, artifact, totalPages, runId) {
  slide.background.fill = c.deep;
  if (!artifact.profile_p_contract && artifact.images?.cover) {
    addImageManaged(slide, artifact.images.cover, "closing-photo", {left:760,top:0,width:520,height:H}, artifact.images.cover_caption, true);
    rect(slide, "closing-source-bg", 770, 655, 490, 50, c.panel);
    text(slide, "closing-source", artifact.images.cover_caption, 780, 661, 470, 38, 17, c.ink, false, "left", f.body, "top");
  }
  if (artifact.profile_p_contract) {
    rect(slide, "closing-accent", 900, 150, 240, 240, "none", c.medium, 1);
    rect(slide, "closing-accent-inner", 940, 190, 160, 160, "none", c.gold, 1);
  }
  text(slide, "closing-kicker", "THANK YOU", 84, 250, 400, 30, 15, c.medium, true, "left", f.subtitle);
  text(slide, "closing-title", "感谢审阅", 84, 300, 600, 90, 48, c.white, true, "left", f.title);
  line(slide, "closing-rule", 84, 420, 220, 2, c.gold, 2);
  text(slide, "closing-sub", `${brief.project_name || ""} · 物业服务方案汇报`, 84, 446, 700, 34, 18, c.light, false, "left", f.body);
  const dateText = new Date().toISOString().slice(0, 10);
  text(slide, "closing-date", dateText, 84, 490, 300, 24, 13, c.medium, false, "left", f.numeric);
  addNotes(slide, "MMF-CLOSING-THEME-D", runId);
}

// Profile-P visual system. Legacy components above keep their previous layout.
function weightedLength(value) {
  return [...String(value || "")].reduce((n, ch) => n + (/^[\x00-\x7F]$/.test(ch) ? 0.56 : 1), 0);
}

function fitSize(value, width, height, preferred, floor = 15) {
  for (let size=preferred; size>floor; size--) {
    const capacity = Math.max(1, Math.floor(width/(size*1.05)));
    const rows = String(value || "").split("\n").reduce((sum, s) => sum + Math.max(1, Math.ceil(weightedLength(s)/capacity)), 0);
    if (rows*size*1.32 <= height) return size;
  }
  return floor;
}

function serviceText(slide, name, value, x, y, w, h, size, color, bold=false, floor=15, align="left") {
  const content=pptTone(value);
  return text(slide,name,content,x,y,w,h,fitSize(content,w,h,size,floor),color,bold,align,bold?f.title:f.body);
}

function serviceHeader(slide, data, pageNo, totalPages, runId) {
  slide.background.fill = c.background;
  rect(slide, "service-left-rule", 60, 35, 5, 52, c.gold);
  serviceText(slide,"service-title",data.title,80,29,1118,66,41,c.deep,true,28);
  if (data.core_message) serviceText(slide,"service-message",data.core_message,80,100,1114,54,23,c.medium,false,17);
  line(slide,"service-header-rule",60,162,1160,1,c.line);
  line(slide,"service-footer-rule",60,664,1160,1,c.line);
  serviceText(slide,"service-project",data._project_name,60,673,840,25,14,c.secondary,false,12);
  text(slide,"service-page",String(pageNo).padStart(2,"0")+" / "+totalPages,1080,673,140,25,15,c.secondary,false,"right");
  addNotes(slide, "PPT-SERVICE-V2", runId);
}

function servicePanel(slide, items, x, y, w, h, name, {dense=false, group=""}={}) {
  rect(slide,name+"-background",x,y,w,h,c.panel,c.line,1);
  const groupHeight=group?65:0, rowH=(h-groupHeight)/items.length;
  if (group) {
    rect(slide,name+"-heading-fill",x,y,w,groupHeight,c.soft);
    serviceText(slide,name+"-heading",group,x+23,y+9,w-46,47,23,c.deep,true);
  }
  items.forEach((item,i)=>{
    const at=y+groupHeight+i*rowH;
    if(i) line(slide,name+"-divider-"+i,x+20,at,w-40,1,c.line);
    const titleH=dense?31:38;
    text(slide,name+"-index-"+i,String(i+1).padStart(2,"0"),x+16,at+12,55,titleH,17,c.medium,true);
    serviceText(slide,name+"-title-"+i,item.title,x+74,at+12,w-97,titleH,dense?22:25,c.deep,true,16);
    serviceText(slide,name+"-body-"+i,item.body,x+74,at+titleH+15,w-97,rowH-titleH-25,dense?20:23,c.ink,false,15);
  });
}

function evidenceDiagram(slide, data, items) {
  const [kind, genericLabel] = serviceLayouts[data.topic_id];
  const label = data.topic_id==="public_interface" && data._evidence?.dual ? "统一管理与独立管理" : genericLabel;
  const bands=["move_in","auth_update","energy","cleaning","enterprise_contact","quality","seasonal"].includes(data.topic_id);
  if(bands && items.length===3) {
    const y=207, width=368, gap=28;
    text(slide,"service-evidence-kicker",kind,60,y,1160,34,22,c.medium,true);
    items.forEach((m,i)=>{
      const x=60+i*(width+gap), accent=i%2?c.primary:c.deep;
      rect(slide,"service-evidence-band-"+i,x,y+54,width,296,c.panel,c.line,1);
      rect(slide,"service-evidence-band-head-"+i,x,y+54,width,83,accent);
      text(slide,"service-evidence-number-"+i,String(i+1).padStart(2,"0"),x+16,y+69,55,45,23,"#E4BF8C",true);
      serviceText(slide,"service-evidence-node-title-"+i,m.title,x+76,y+61,width-94,68,26,c.white,true,17);
      serviceText(slide,"service-evidence-node-body-"+i,m.body,x+25,y+152,width-50,169,24,c.ink,false,16);
    });
    line(slide,"service-evidence-band-line",60,y+377,1160,1,c.line,1);
    return;
  }
  const h = 444;
  rect(slide,"service-evidence-rail",60,192,254,h,c.deep);
  serviceText(slide,"service-evidence-kicker",kind,84,219,204,56,20,"#E4BF8C",true);
  serviceText(slide,"service-evidence-topic",label,84,280,204,134,33,c.white,true,24);
  const n=items.length, gap=13, rowH=(h-(n-1)*gap)/n;
  items.forEach((item,i)=>{
    const y=192+i*(rowH+gap), accent=i%2?c.gold:c.primary;
    line(slide,"service-evidence-connector-"+i,314,y+rowH/2,40,1,accent,2);
    rect(slide,"service-evidence-row-"+i,354,y,866,rowH,c.panel,c.line,1);
    rect(slide,"service-evidence-node-"+i,354,y,7,rowH,accent);
    const titleW=205;
    serviceText(slide,"service-evidence-node-title-"+i,item.title,382,y+12,titleW,rowH-24,25,c.deep,true,17);
    line(slide,"service-evidence-separator-"+i,609,y+18,1,rowH-36,c.line);
    serviceText(slide,"service-evidence-node-body-"+i,item.body,631,y+12,563,rowH-24,23,c.ink,false,17);
  });
}

function renderServiceCharts(slide, data) {
  const ev=data._evidence, charts=ev.charts;
  const funding=ev.funding;
  servicePanel(slide,data.modules,60,186,516,447,"service-overview",{dense:true});
  rect(slide,"service-metrics-shell",600,186,620,447,c.panel,c.line,1);
  const top=funding?267:198, tall=(618-top)/charts.length;
  if(funding) {
    rect(slide,"service-metrics-banner",600,186,620,66,c.deep);
    text(slide,"service-funding-label","项目简介 · 计划投资",623,205,320,28,20,c.light);
    text(slide,"service-funding-number",funding+" 亿元",943,194,249,47,32,c.white,true,"right");
  }
  charts.forEach((metric,i)=>{
    const y=top+i*tall;
    if(i) line(slide,"metric-divider-"+i,621,y-4,581,1,c.line);
    text(slide,"metric-title-"+i,metric.title,622,y+4,570,29,23,c.deep,true);
    slide.charts.add("bar",{
      position:{left:616,top:y+31,width:583,height:tall-57},
      categories:metric.categories,
      series:[{name:metric.unit,values:metric.values,fill:c.primary,
        valuesFormatCode:metric.values.every(Number.isInteger) ? "#,##0" : "0.0#",
        points:metric.values.map((_,idx)=>({idx,fill:idx%2?c.gold:c.primary}))}],
      barOptions:{direction:"bar",grouping:"clustered",gapWidth:60},
      hasLegend:false,chartFill:c.panel,plotAreaFill:c.panel,
      xAxis:{textStyle:{fontSize:15,fill:c.ink},line:{fill:c.line,width:1}},
      yAxis:{min:0,max:Math.max(...metric.values)*1.25,tickLabelPosition:"none",majorGridlines:null},
      dataLabels:{showValue:true,position:"outEnd",textStyle:{fontSize:16,fill:c.deep,bold:true}},
    });
  });
  text(slide,"metric-basis","数据口径：依据园区规划建筑面积与单体楼宇建设指标汇总",622,590,570,28,14,c.medium);
}

function nativeServiceTable(slide, columns, rows, x, y, w, maxHeight, options = {}) {
  if (!columns.length || !rows.length || rows.some(r=>r.length!==columns.length)) {
    throw new Error("Profile-P table requires consistent native rows and columns");
  }
  const values=[columns,...rows].map(row=>row.map(pptTone));
  const weights=columns.map((_,col)=>Math.max(1,Math.sqrt(Math.max(...values.map(row=>weightedLength(row[col]))))));
  const widths=weights.map(wt=>w*wt/weights.reduce((a,b)=>a+b,0));
  let sizes, heights;
  for (let bodySize=22;bodySize>=14;bodySize--) {
    sizes=values.map((_,r)=>r===0?Math.min(20,bodySize):bodySize);
    heights=values.map((row,r)=>{
      const sz=sizes[r];
      const lines=Math.max(...row.map((cell,i)=>String(cell).split("\n").reduce((k,s)=>
        k+Math.max(1,Math.ceil(weightedLength(s)/Math.max(1,Math.floor((widths[i]-32)/(sz*1.08))))),0)));
      const pad = rows.length === 3 ? 19 : 12;
      return Math.max(r===0?55:rows.length<=3?100:70, Math.ceil(lines*sz*1.43+pad*2));
    });
    if (heights.reduce((a,b)=>a+b,0)<=maxHeight) break;
  }
  let dh=heights.reduce((a,b)=>a+b,0);
  if (rows.length === 3 && options.stretchBottom) {
    const target = Math.min(maxHeight, options.stretchBottom - y);
    if (dh < target) {
      const extra = (target - dh) / (heights.length - 1);
      for (let r = 1; r < heights.length; r++) heights[r] += extra;
      dh = target;
    }
  }
  if (dh>maxHeight) throw new Error("Profile-P table text exceeds the safe layout height");
  const table=slide.tables.add({rows:values.length,columns:columns.length,left:x,top:y,width:w,height:dh,values,columnWidths:widths});
  table.styleOptions={headerRow:false,bandedRows:false,firstColumn:false};
  table.borders.assign({style:"solid",fill:c.line,width:1});
  for (let r=0;r<values.length;r++) {
    table.rows[r].height=heights[r];
    for(let col=0;col<columns.length;col++) {
      const cell=table.getCell(r,col);
      cell.fill=r===0?c.deep:r%2===0?c.soft:c.panel;
      cell.text.style={fontSize:sizes[r],bold:r===0||col===0,color:r===0?c.white:c.ink,fontFamily:f.body,typeface:f.body,
        alignment:"left",verticalAlignment:"middle",margin:rows.length===3?19:12,wrap:true};
    }
  }
  return y + dh;
}

export function renderServiceSlide(slide, data, pageNo, totalPages, route, runId) {
  serviceHeader(slide,data,pageNo,totalPages,runId);
  if (Array.isArray(data._navigation)) {
    const scope=data._evidence?.scope || [], chapters=data._navigation;
    const navigationWidth=scope.length ? 642 : 1160;
    const rowH=Math.min(77,444/Math.max(1,chapters.length));
    chapters.forEach((chapter,i)=>{
      const y=190+i*rowH;
      line(slide,"service-nav-line-"+i,60,y,navigationWidth,1,c.line);
      text(slide,"service-nav-no-"+i,String(i+1).padStart(2,"0"),64,y+8,53,rowH-16,20,c.primary,true);
      serviceText(slide,"service-nav-title-"+i,chapter.title,124,y+6,224,rowH-12,21,c.deep,true,15);
      serviceText(slide,"service-nav-goal-"+i,chapter.goal,362,y+6,navigationWidth-304,rowH-12,19,c.ink,false,15);
    });
    if(scope.length) servicePanel(slide,scope,729,190,491,446,"service-scope",{group:"项目资料列明的服务板块",dense:true});
    return;
  }

  if (data.layout === "table") {
    const columns=data.table?.columns || [], rows=data.table?.rows || [];
    const ev=data._evidence || {};
    const details=data.topic_id==="customer_groups" ? [
      ...(ev.background ? [{title:"合作背景",body:ev.background}] : []),
      ...(ev.bases ? [{title:"规划建设方向",body:ev.bases}] : []),
    ] : [];
    const beside=details.length && columns.length<=3 && rows.length<=5;
    if (beside) servicePanel(slide,details,846,189,374,446,"service-brief",{group:"项目简介",dense:true});
    const shortTable = rows.length === 3 && !beside;
    const tableTop = 189;
    const tableBottom = nativeServiceTable(slide,columns,rows,60,tableTop,beside?760:1160,447, shortTable ? {stretchBottom: 5.78 * 96} : {});
    if (shortTable && tableBottom <= 5.85 * 96) {
      const barY = 5.85 * 96;
      const barH = (6.60 - 5.85) * 96;
      rect(slide,"service-table-note",60,barY,1160,barH,c.soft,c.line,1);
      rect(slide,"service-table-note-bar",60,barY,8,barH,c.deep);
      text(slide,"service-table-note-title","本页执行要点 / 接口说明",84,barY+8,1120,24,16,c.deep,true);
      serviceText(slide,"service-table-note-body",data.core_message||"本页按阶段写明责任岗位、交接条件和可以核对的结果。",84,barY+32,1120,barH-40,15,c.ink,false,13);
    }
    return;
  }

  if (data.layout === "process") {
    const steps=data.steps || [];
    if(steps.length<2 || steps.length>5) throw new Error("Profile-P process needs 2-5 actual business steps");
    const gap=32, y=212, height=365, w=(1160-gap*(steps.length-1))/steps.length;
    steps.forEach((s,i)=>{
      const x=60+i*(w+gap), accent=i===steps.length-1?c.primary:c.deep;
      rect(slide,"service-step-card-"+i,x,y,w,height,c.panel,c.line,1);
      rect(slide,"service-step-header-"+i,x,y,w,78,accent);
      text(slide,"service-step-no-"+i,String(i+1).padStart(2,"0"),x+17,y+12,w-34,54,33,"#E4BF8C",true);
      serviceText(slide,"service-step-title-"+i,s.title,x+17,y+90,w-34,76,25,c.deep,true,17);
      serviceText(slide,"service-step-body-"+i,s.body,x+17,y+180,w-34,158,22,c.ink,false,16);
      if(i<steps.length-1) shape(slide,"rightArrow","service-flow-"+i,
        {left:x+w+(gap-19)/2,top:y+160,width:19,height:16},c.primary,c.primary,0);
    });
    return;
  }

  const modules=data.modules || [];
  if(modules.length<2 || modules.length>4) throw new Error("Profile-P modules require 2-4 actual service arrangements");
  if(data.topic_id==="project_overview" && data._evidence?.charts.length && !data._image) {
    renderServiceCharts(slide,data);
    return;
  }
  if (data._image) {
    const right=data.image_side==="right", x=right?709:60, w=511;
    rect(slide,"service-photo-frame-"+pageNo,x,189,w,447,c.panel,c.line,1);
    const fitted=fitInside(data._image,{left:x+12,top:201,width:w-24,height:382});
    addImageManaged(slide,data._image,"service-photo-"+pageNo,fitted,data.image_caption||"服务场景示意",true);
    serviceText(slide,"service-image-caption",data.image_caption||"服务场景示意",fitted.left,592,fitted.width,32,16,c.medium);
    servicePanel(slide,modules,right?60:594,189,626,447,"service-image-module",{dense:true});
    return;
  }
  if (Object.hasOwn(serviceLayouts,data.topic_id)) {
    evidenceDiagram(slide,data,modules);
    return;
  }
  // Short items get room to breathe without drawing an empty full-height card.
  const cols=modules.length===4?2:modules.length, rows=Math.ceil(modules.length/cols);
  const w=(1160-(cols-1)*24)/cols, h=rows===2?207:330, y0=rows===2?194:228;
  modules.forEach((m,i)=>{
    const x=60+i%cols*(w+24), y=y0+Math.floor(i/cols)*(h+18);
    rect(slide,"service-module-card-"+i,x,y,w,h,c.panel,c.line,1);
    rect(slide,"service-module-bar-"+i,x,y,w,5,i%2?c.gold:c.primary);
    text(slide,"service-module-index-"+i,String(i+1).padStart(2,"0"),x+22,y+15,60,40,26,c.medium,true);
    serviceText(slide,"service-module-title-"+i,m.title,x+22,y+61,w-44,51,26,c.deep,true,17);
    serviceText(slide,"service-module-body-"+i,m.body,x+22,y+118,w-44,h-138,23,c.ink,false,16);
  });
}

export function renderOverview(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const bullets = (data.bullets || []).slice(0, 5);
  const imgPath = data._image;
  const textRight = imgPath ? 780 : 1060;
  if (imgPath) {
    addImageManaged(slide, imgPath, `overview-photo-${pageNo}`, { left: 812, top: 130, width: 408, height: 520 }, "章节配图");
    round(slide, "overview-photo-frame", 812, 130, 408, 520, "none", c.line, 1);
  }
  if (!bullets.length) {
    const message = data.core_message || "";
    text(slide, "overview-hero", message, 84, 240, textRight - 120, 200, 28, c.deep, true, "left", f.title, "top");
    line(slide, "overview-hero-rule", 84, 470, 220, 2, c.gold, 2);
    return;
  }
  const top = 194;
  const rowGap = bullets.length > 3 ? 66 : 78;
  bullets.forEach((item, index) => {
    const y = top + index * rowGap;
    round(slide, `overview-no-bg-${index}`, 84, y, 52, 38, index === 0 ? c.deep : c.soft, index === 0 ? c.deep : c.line, 1);
    text(slide, `overview-no-${index}`, String(index + 1).padStart(2, "0"), 84, y + 7, 52, 22, 12, index === 0 ? c.white : c.primary, true, "center", f.numeric);
    text(slide, `overview-text-${index}`, item, 162, y - 3, textRight - 162, 48, 20, c.ink, index === 0, "left", f.body);
    if (index < bullets.length - 1) line(slide, `overview-rule-${index}`, 162, y + 58, textRight - 162, 1, c.line, 1);
  });
}
