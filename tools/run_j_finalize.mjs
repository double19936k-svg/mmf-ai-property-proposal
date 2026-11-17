// Artifact validation for the offline Run J deck; no provider or scheduler.
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [workspaceDir, skillDir, pythonExecutable, evidenceDir] = process.argv.slice(2);
if (![workspaceDir,skillDir,pythonExecutable].every(p=>p && path.isAbsolute(p))) throw new Error("three absolute paths required");
const { finalizePresentation } = await import(pathToFileURL(path.join(skillDir,"container_tools/artifact_tool_utils.mjs")).href);
const evidence=evidenceDir || path.join(workspaceDir,"runtime/run_j/evidence");
const relativeEvidence=path.relative(workspaceDir,evidence);
if (!path.isAbsolute(evidence) || path.isAbsolute(relativeEvidence) || relativeEvidence.startsWith("..")) throw new Error("evidence must be inside the Run J workspace");
const reportDir=evidenceDir ? path.join(path.dirname(evidence),"finalizer") : path.join(workspaceDir,"runtime/run_j/finalizer");
const contract=JSON.parse(await fs.readFile(path.join(evidence,"c4_insertion_plan.json"),"utf8"));
await fs.mkdir(reportDir,{recursive:true});
const result=await finalizePresentation({
  workspaceDir,
  candidatePath:path.join(evidence,"RUN_J_VISUAL_SAMPLES_DRAFT.pptx"),
  finalPath:path.join(evidence,"RUN_J_VISUAL_SAMPLES.pptx"),
  pythonExecutable,
  integrityValidatorPath:path.join(skillDir,"container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath:path.join(skillDir,"container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs:["--expected-slide-size-emu","12192000,6858000","--validate-bullet-geometry","--validate-heading-fit"],
  explicitTotalSlideCount:contract.predicted_physical_slide_count,
  requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],
  fontPolicy:{basis:"design",families:["Microsoft YaHei","DengXian"]},
  verifyArtifactToolImport:true,
  receiptPath:path.join(reportDir,"validation.json"),
});
console.log(JSON.stringify(result));
