"""Initialize/read the PPT instance clock without calling any model or resetting it."""
from __future__ import annotations
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
from planning.ppt_delivery import execution_budget,estimated_seconds

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--run-root",type=Path,required=True)
    p.add_argument("--body-pages",type=int,required=True)
    p.add_argument("--chapters",type=int,required=True)
    p.add_argument("--page-p90-seconds",type=float)
    args=p.parse_args()
    if not args.run_root.is_dir():p.error("create the new isolated run directory first")
    physical=args.body_pages+args.chapters+3
    if not 1<=args.body_pages or not 0<=args.chapters or physical>60:p.error("plan exceeds 60 physical pages")
    budget=execution_budget(args.run_root/"longform/ppt")
    now=time.time();result={**budget,"physical_pages":physical,"elapsed_seconds":round(now-budget["started_epoch"],3),"remaining_wall_seconds":max(0,budget["deadline_epoch"]-now),"external_model_calls":0}
    if args.page_p90_seconds is not None:
        eta=estimated_seconds(args.body_pages,args.page_p90_seconds)
        result.update(estimated_total_seconds=eta,capacity_estimate="WITHIN_90MIN" if eta<=5400 else "AT_RISK",estimate_is_acceptance=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if now<budget["deadline_epoch"] else 2

if __name__=="__main__":raise SystemExit(main())
