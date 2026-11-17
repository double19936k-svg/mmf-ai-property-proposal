// Only explicit, compatible statements in the input brief may become numeric charts.
// A page about energy management does not imply metered consumption or savings.
const briefs = b => [b.requirements, b.functional_composition].filter(x => typeof x === "string");
const digits = "[0-9][0-9,，]*(?:\\.[0-9]+)?";
const unit = "(?:平方米|㎡|m[²2])";
const amount = new RegExp(`(?:总建筑面积|总建面|建筑面积)(?:为|约为|约)?\\s*(${digits})\\s*${unit}\\s*[，,、](?:其中\\s*)?地上(?:建筑)?面积(?:为|约为|约)?\\s*(${digits})\\s*${unit}`, "g");
function values(match) {
  return [...match].slice(1).map(s => Number(s.replace(/[,，]/g, "")));
}
function consensus(rows) {
  const distinct = new Map(rows.map(row => [JSON.stringify(row), row]));
  return distinct.size === 1 ? [...distinct.values()][0] : null;
}
export function projectEvidence(brief = {}) {
  const source = briefs(brief).map(s => s.normalize("NFKC").replaceAll("m2", "m²"));
  const area = consensus(source.flatMap(s => [...s.matchAll(amount)].map(values)));
  // Same scoped phrase for each tower; no generic match on a random floor number.
  const floorsPattern = /(?:包含|含|包括)\s*1栋\s*(\d{1,2})层\s*(?:研发)?办公楼[、，,及和\s]+1栋\s*(\d{1,2})层\s*(?:酒店式)?公寓[、，,及和以\s]+1栋\s*(\d{1,2})层\s*(?:高标准)?(?:工业)?厂房及数据中心/g;
  const floors = consensus(source.flatMap(s => [...s.matchAll(floorsPattern)].map(values)));
  const charts = [];
  if (area && area[0] > area[1] && area[1] > 0) {
    const scale = area.every(v=>v>=10000 && Number.isInteger(v/100)) ? 10000 : 1;
    const unit = scale===10000 ? "万㎡" : "㎡";
    charts.push({title: `建筑面积约值（${unit}）`, categories: ["总建筑面积", "其中地上面积"],
      values: area.map(v=>v/scale), basis: "", unit});
  }
  if (floors && floors.every(v => v > 0)) charts.push({
    title: "楼宇层数（层）", categories: ["办公楼", "公寓", "厂房及数据中心"], values: floors,
    basis: "", unit: "层",
  });
  const funding = consensus(source.flatMap(s => [...s.matchAll(/(?:拟|计划)\s*投资\s*(\d+(?:\.\d+)?)\s*亿元/g)].map(m => m[1])));
  const scopeLabels = ["常规性物业服务", "办公空间综合设施服务", "增值服务"];
  const scope = scopeLabels.flatMap(label => {
    const line = consensus(source.flatMap(s => [...s.matchAll(new RegExp(
      `(?:^|[\\n;；。])\\s*${label}[:：]\\s*(?:包括)?([^\\n;；。]{2,110})`, "g"
    ))].map(m => m[1].trim().replace(/等[、,，]?$/, ""))));
    return line ? [{ title: label, body: line }] : [];
  });
  const background = source.flatMap(s => s.split(/[。\n]/)).filter(s =>
    /签署了?《[^》]{2,35}》/.test(s) && s.length <= 110);
  const bases = source.flatMap(s => s.split(/[。\n]/)).filter(s =>
    /(?:打造|建设).+基地/.test(s) && (s.match(/(?:打造|建设)/g) || []).length >= 3)
    .map(s => s.split(/(?:未来将|拟|计划)打造/).at(-1))
    .filter(s => s.length <= 145)
    .map(s => s.replace(/(?:^|、|以及)\s*(?:打造|建设)/g, "、").replace(/^、/, "").replace(/以及建设/, "、"));
  const dual = source.some(s=>/统一管理与独立管理/.test(s));
  return { charts, funding, scope, dual, background: consensus(background), bases: consensus(bases) };
}

// The registry describes only the visual grouping. All node labels and explanations
// come from the page payload. With unfamiliar topics, render the regular layouts.
export const layouts = Object.freeze({
  public_interface: ["责任分界", "按对象分工"], handover: ["接管查验", "查验与交付"],
  move_in: ["入驻接口", "企业入驻"], fitout: ["施工接口", "进场与验收"],
  layout_zones: ["空间分区", "分区服务"], access: ["通行权限", "人员与访客"],
  logistics: ["货运组织", "场地与时段"], elevator_split: ["分流安排", "客流与货流"],
  auth_update: ["授权管理", "有效权限"], maintenance: ["巡检维保", "日常与专项"],
  energy: ["分项管理", "读数与投入"], cleaning: ["场景管理", "分类保洁"],
  amenities: ["配套界面", "按业态协同"], waste_stream: ["清运管理", "分类与场地"],
  enterprise_contact: ["企业联络", "沟通与协同"], seasonal: ["养护安排", "按场景排期"],
  quality: ["品质检查", "发现与复核"], vacancy: ["运营交接", "变更与衔接"],
});
