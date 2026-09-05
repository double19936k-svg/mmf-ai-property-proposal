import { THEME, W, H } from "./theme.mjs";

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

function text(slide, name, value, left, top, width, height, size, color, bold = false, alignment = "left", fontFamily = f.body, verticalAlignment = "middle") {
  const item = shape(slide, "textbox", name, { left, top, width, height });
  item.text = String(value ?? "");
  item.text.style = { fontSize: size, color, bold, alignment, verticalAlignment, margin: 0, wrap: true, fontFamily, typeface: fontFamily };
  return item;
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
  text(slide, `kicker-${pageNo}`, `物业服务方案 · ${data._scenario || "方案章节"}`, 76, 20, 550, 22, 13, c.secondary, true, "left", f.subtitle);
  text(slide, `title-${pageNo}`, data.title, 76, 44, 1060, 46, THEME.typography.slideTitle, c.ink, true, "left", f.title);
  line(slide, `header-rule-${pageNo}`, 60, 100, 1160, 1, c.line, 1);
  text(slide, `lead-${pageNo}`, data.core_message || "", 76, 111, 1085, 42, THEME.typography.lead, c.muted, false, "left", f.body);
  text(slide, `page-${pageNo}`, `${String(pageNo).padStart(2, "0")} / ${String(totalPages).padStart(2, "0")}`, 1085, 677, 135, 18, 10, "#819196", false, "right", f.numeric);
  text(slide, `project-${pageNo}`, data._project_name || "", 60, 677, 420, 18, 10, "#819196", false, "left", f.body);
  addNotes(slide, componentId, runId);
}

export function renderCover(slide, brief, artifact, totalPages, runId) {
  slide.background.fill = c.background;
  rect(slide, "cover-rail", 0, 0, 178, H, c.deep);
  rect(slide, "cover-band", 178, 0, 16, H, c.primary);
  text(slide, "cover-kicker", "PROPERTY SERVICE PROPOSAL", 238, 78, 580, 28, 14, c.secondary, true, "left", f.subtitle);
  text(slide, "cover-project", brief.project_name, 238, 118, 850, 38, 22, c.primary, true, "left", f.body);
  text(slide, "cover-title", artifact.title, 238, 176, 880, 142, THEME.typography.deckTitle, c.deep, true, "left", f.title, "top");
  line(slide, "cover-rule", 238, 340, 760, 1, c.line, 2);
  text(slide, "cover-subtitle", artifact.subtitle || `${brief.project_type}｜${brief.scenario}`, 238, 366, 820, 50, 24, c.secondary, false, "left", f.subtitle);
  round(slide, "cover-meta", 238, 520, 720, 92, c.panel, c.line, 1);
  text(slide, "cover-meta-label", "本章聚焦", 266, 540, 100, 24, 14, c.secondary, true, "left", f.subtitle);
  text(slide, "cover-meta-value", brief.requirements || brief.scenario, 370, 532, 560, 42, 18, c.ink, true, "left", f.body);
  text(slide, "cover-medium", "物业方案章节初稿", 266, 578, 300, 18, 13, c.primary, true, "left", f.body);
  text(slide, "cover-page", `01 / ${String(totalPages).padStart(2, "0")}`, 1085, 677, 135, 18, 10, "#819196", false, "right", f.numeric);
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
  const region = { left: 60, right: 1220, top: 178, bottom: 560 };
  const rowGap = 22;
  const rowHeight = rows.length === 1 ? 300 : 182;
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
  if (data.bullets?.length) {
    rect(slide, "modules-context", 60, 584, 1160, 58, c.primary, c.primary, 0);
    text(slide, "modules-context-label", "项目语境", 84, 599, 95, 22, 13, c.light, true, "left", f.subtitle);
    text(slide, "modules-context-value", data.bullets.slice(0, 3).join("  ｜  "), 190, 592, 1000, 38, 14, c.white, false, "center", f.body);
  } else {
    rect(slide, "modules-context", 60, 584, 1160, 58, c.deep, c.deep, 0);
    text(slide, "modules-context-label", "服务要求", 84, 599, 95, 22, 13, c.light, true, "left", f.subtitle);
    text(slide, "modules-context-value", data.core_message || "", 190, 592, 1000, 38, 14, c.white, false, "center", f.body);
  }
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
  rect(slide, "process-principle", 60, 572, 1160, 54, c.deep, c.deep, 0);
  text(slide, "process-principle-label", "协同原则", 84, 588, 96, 20, 13, c.light, true, "left", f.subtitle);
  text(slide, "process-principle-value", data.core_message || "", 190, 580, 1000, 36, 14, c.white, false, "center", f.body);
}

export function renderTable(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const columns = data.table?.columns || [];
  const rows = (data.table?.rows || []).slice(0, 7);
  if (!columns.length || !rows.length) return renderOverview(slide, data, pageNo, totalPages, route, runId);
  const values = [columns, ...rows];
  const columnTracks = columns.map((_, index) => ({ mode: "fr", value: index === 0 ? 1.15 : index === columns.length - 1 ? 0.95 : 2.35 }));
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
  rect(slide, "table-conclusion", 60, 582, 1160, 54, c.deep, c.deep, 0);
  text(slide, "table-conclusion-label", "执行重点", 84, 598, 96, 20, 13, c.light, true, "left", f.subtitle);
  text(slide, "table-conclusion-value", data.core_message || "", 190, 590, 1000, 36, 14, c.white, false, "right", f.body);
}

export function renderOverview(slide, data, pageNo, totalPages, route, runId) {
  addHeader(slide, data, pageNo, totalPages, route.component_id, runId);
  const bullets = (data.bullets || []).slice(0, 5);
  const top = 194;
  bullets.forEach((item, index) => {
    const y = top + index * 78;
    round(slide, `overview-no-bg-${index}`, 84, y, 52, 38, index === 0 ? c.deep : c.soft, index === 0 ? c.deep : c.line, 1);
    text(slide, `overview-no-${index}`, String(index + 1).padStart(2, "0"), 84, y + 7, 52, 22, 12, index === 0 ? c.white : c.primary, true, "center", f.numeric);
    text(slide, `overview-text-${index}`, item, 162, y - 3, 980, 48, 20, c.ink, index === 0, "left", f.body);
    if (index < bullets.length - 1) line(slide, `overview-rule-${index}`, 162, y + 58, 980, 1, c.line, 1);
  });
}
