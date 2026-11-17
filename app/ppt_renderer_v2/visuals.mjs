// Draw only the target-independent scene. No business extraction or guessing.
import crypto from "node:crypto";

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]));
  return value;
}

export function renderVisualScene(slide, data, pageNo, totalPages) {
  const scene = data.visual_scene;
  if (scene?.schema_version !== "mmf-visual-scene-v1" || !scene.semantic_ir) throw new Error("validated visual scene required");
  const computed = crypto.createHash("sha256").update(JSON.stringify(stable(scene.semantic_ir))).digest("hex");
  if (computed !== scene.semantic_digest) throw new Error("visual semantic digest mismatch");
  const geometry = Object.fromEntries(["width","height","primitives","semantic_digest"].map(k => [k,scene[k]]));
  if (crypto.createHash("sha256").update(JSON.stringify(stable(geometry))).digest("hex") !== scene.scene_digest) {
    throw new Error("visual scene digest mismatch");
  }
  const factor = Math.min(1120 / scene.width, 470 / scene.height);
  const ox = (1280 - scene.width * factor) / 2, oy = 175;
  const rect = (id, x, y, w, h, fill, stroke = "none") => slide.shapes.add({
    name: id, geometry: "rect", position: {left:x, top:y, width:w, height:h},
    fill, line: {fill:stroke, width: stroke === "none" ? 0 : 1},
  });
  const text = (id, value, x, y, w, h, size, bold=false) => {
    const item = slide.shapes.add({name:id, geometry:"textbox", position:{left:x,top:y,width:w,height:h},
      fill:"none",line:{fill:"none",width:0}});
    item.text = value;
    item.text.style = {typeface:"Microsoft YaHei", fontFamily:"Microsoft YaHei",fontSize:size,
      color:"#222222",bold,alignment:"center",verticalAlignment:"middle",
      insets:{left:0,right:0,top:0,bottom:0},wrap:"none",autoFit:"none"};
    return item;
  };
  slide.background.fill = "#FFFFFF";
  text("visual-title", data.title, 70, 40, 1140, 58, 35, true);
  text("visual-caption", data.core_message || "", 70, 108, 1140, 40, 18);
  for (const item of scene.primitives) {
    if (item.type === "box") {
      rect("visual:" + item.id, ox+item.x*factor, oy+item.y*factor, item.w*factor,item.h*factor,item.fill,item.border===false?"none":"#444444");
      text("label:" + item.id, item.lines.join("\n"), ox+(item.x+10)*factor,oy+(item.y+5)*factor,
        (item.w-20)*factor,(item.h-10)*factor,item.font_size*factor,item.bold);
    } else if (item.type === "edge") {
      for (let i=1; i<item.points.length; i++) {
        const a=item.points[i-1], b=item.points[i];
        const x1=ox+a[0]*factor,y1=oy+a[1]*factor,x2=ox+b[0]*factor,y2=oy+b[1]*factor;
        if (x1===x2 && y1===y2) continue;
        // Ordered connector endpoints preserve upward/leftward arrow direction.
        const from = rect(`anchor:${item.id}:${i}:a`,x1-.05,y1-.05,.1,.1,"none");
        const to = rect(`anchor:${item.id}:${i}:b`,x2-.05,y2-.05,.1,.1,"none");
        slide.shapes.connect(from,to,{kind:"straight",
          line:{fill:"#444444",width:1.7,style:item.dashed?"dashed":"solid"},
          tail:i===item.points.length-1?{type:"triangle",width:"med",length:"med"}:{type:"none"}});
      }
    } else throw new Error("unsupported scene primitive");
  }
  text("visual-page", `${pageNo} / ${totalPages}`, 1100,677,110,20,11);
  text("visual-project", data._project_name || "", 60,677,750,20,11);
  // Full canonical semantics and original evidence survive editable PPT export.
  slide.speakerNotes.textFrame.setText("[MMF_VISUAL_IR_V1]\n" + JSON.stringify(scene.semantic_ir) +
    "\n[SEMANTIC_DIGEST]\n" + scene.semantic_digest);
}
