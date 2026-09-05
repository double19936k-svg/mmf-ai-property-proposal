const COMPONENTS = Object.freeze({
  cover: "MMF-COVER-THEME-D",
  overview: "SK-03-DERIVED-SUMMARY",
  modules_3: "SK-06-CARDS-3",
  modules_4: "SK-06-CARDS-4",
  modules_grid: "SK-06-CARDS-RESPONSIVE",
  process_3: "SK-07-FLOW-3",
  process_4: "SK-07-FLOW-4",
  process_5: "SK-07-FLOW-5",
  process_branch: "SK-08-COMPLEX-FLOW",
  table_standard: "SK-13-NATIVE-TABLE",
  table_split: "SK-13-NATIVE-TABLE-SPLIT",
  matrix: "SK-16-MATRIX",
  comparison: "SK-13-COMPARISON-TABLE"
});

function maxTextLength(items, fields) {
  return Math.max(0, ...items.flatMap((item) => fields.map((field) => [...String(item?.[field] || "")].length)));
}

export function routeSlide(data) {
  const semantic = data.layout || "overview";
  if (semantic === "modules") {
    const count = Array.isArray(data.modules) ? data.modules.length : 0;
    const density = maxTextLength(data.modules || [], ["title", "body"]);
    const componentId = count === 3 ? COMPONENTS.modules_3 : count === 4 ? COMPONENTS.modules_4 : COMPONENTS.modules_grid;
    return { semantic_layout: semantic, component_id: componentId, density, item_count: count, reuse_basis: "SK-06 frozen E3" };
  }
  if (semantic === "process" || semantic === "timeline") {
    const steps = data.steps || [];
    const hasBranch = steps.some((step) => step.branch || step.return_to || step.relation === "exception_branch");
    if (hasBranch) return { semantic_layout: semantic, component_id: COMPONENTS.process_branch, item_count: steps.length, reuse_basis: "SK-08 frozen E3" };
    const componentId = steps.length <= 3 ? COMPONENTS.process_3 : steps.length === 4 ? COMPONENTS.process_4 : COMPONENTS.process_5;
    return { semantic_layout: semantic, component_id: componentId, item_count: steps.length, reuse_basis: "SK-07 frozen E3" };
  }
  if (["table", "responsibility_matrix", "comparison"].includes(semantic)) {
    const rows = data.table?.rows?.length || 0;
    const columns = data.table?.columns?.length || 0;
    const componentId = semantic === "comparison" ? COMPONENTS.comparison : rows > 7 ? COMPONENTS.table_split : COMPONENTS.table_standard;
    return { semantic_layout: semantic, component_id: componentId, rows, columns, reuse_basis: "SK-13 frozen native-table pattern" };
  }
  if (semantic === "matrix") return { semantic_layout: semantic, component_id: COMPONENTS.matrix, reuse_basis: "SK-16 frozen E3" };
  return { semantic_layout: semantic, component_id: COMPONENTS.overview, reuse_basis: "SK-03 frozen pattern" };
}

export const componentRegistry = COMPONENTS;
