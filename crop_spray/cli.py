"""植保员命令行。

账本路径统一由 --ledger 指定（默认 runtime/spray_ledger.jsonl）。
结构化入参（航迹点、处方等）用 --json 指向 JSON 文件，简单参数直接给标志。

示例：
    python3 -m crop_spray.cli sync --task TASK-B --leg B-L1 \
        --batch SYNC-B-1 --json reference/track_sample_b.json
    python3 -m crop_spray.cli trace COMP-9
    python3 -m crop_spray.cli export TASK-B
    python3 -m crop_spray.cli verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import Ledger, SprayService

DEFAULT_LEDGER = "runtime/spray_ledger.jsonl"


def load_json(value: str):
    p = Path(value)
    return json.loads(p.read_text(encoding="utf-8"))


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crop_spray.cli", description="县级植保作业台")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, **kw)

    p = add("register-field"); p.add_argument("--json", required=True)
    p = add("register-sensitive"); p.add_argument("--json", required=True)
    p = add("register-label"); p.add_argument("--json", required=True)
    p = add("declare-batch")
    p.add_argument("--batch", required=True); p.add_argument("--chemical", required=True)
    p.add_argument("--qty-ml", type=float, required=True); p.add_argument("--actor", default="仓管")

    p = add("declare-planting")
    for f in ("--declaration", "--field", "--crop", "--applicant", "--season"):
        p.add_argument(f, required=True)

    p = add("submit-rx"); p.add_argument("--json", required=True); p.add_argument("--actor", required=True)
    p = add("approve-rx"); p.add_argument("--rx", required=True)
    p.add_argument("--reviewer", required=True); p.add_argument("--at", required=True)

    p = add("create-task"); p.add_argument("--task", required=True)
    p.add_argument("--rx", required=True); p.add_argument("--at", required=True)
    p.add_argument("--parent", default=None, help="补飞时填原任务号")

    p = add("load-tank"); p.add_argument("--tank", required=True)
    p.add_argument("--batch", required=True); p.add_argument("--task", required=True)
    p.add_argument("--at", required=True); p.add_argument("--actor", default="加药员")

    p = add("weather"); p.add_argument("--task", required=True)
    p.add_argument("--at", required=True); p.add_argument("--wind", type=float, required=True)
    p.add_argument("--rain", type=float, required=True)

    p = add("takeoff"); p.add_argument("--task", required=True)
    p.add_argument("--leg", required=True); p.add_argument("--at", required=True)
    p.add_argument("--actor", default="飞手")

    p = add("decide"); p.add_argument("--task", required=True)
    p.add_argument("--leg", required=True); p.add_argument("--at", required=True)
    p.add_argument("--decision", choices=["RETURN", "CONTINUE"], required=True)
    p.add_argument("--by", required=True); p.add_argument("--note", default="")

    p = add("land"); p.add_argument("--task", required=True)
    p.add_argument("--leg", required=True); p.add_argument("--at", required=True)

    p = add("sync"); p.add_argument("--task", required=True)
    p.add_argument("--leg", required=True); p.add_argument("--batch", required=True)
    p.add_argument("--json", required=True)

    p = add("analyze"); p.add_argument("--task", required=True); p.add_argument("--leg", required=True)
    p = add("seal"); p.add_argument("--task", required=True)
    p.add_argument("--by", required=True); p.add_argument("--at", required=True)
    p = add("addendum"); p.add_argument("--task", required=True)
    p.add_argument("--by", required=True); p.add_argument("--at", required=True)

    p = add("complaint")
    for f in ("--id", "--task", "--at", "--complainant", "--allegation"):
        p.add_argument(f, required=True)

    p = add("trace"); p.add_argument("complaint")
    p = add("export"); p.add_argument("task")
    add("verify")

    args = parser.parse_args(argv)
    svc = SprayService(Ledger(args.ledger))
    c = args.cmd

    try:
        if c == "register-field":
            svc.register_field(load_json(args.json))
        elif c == "register-sensitive":
            svc.register_sensitive_area(load_json(args.json))
        elif c == "register-label":
            svc.register_label(load_json(args.json))
        elif c == "declare-batch":
            svc.declare_batch(args.batch, args.chemical, args.qty_ml, actor=args.actor)
        elif c == "declare-planting":
            svc.declare_field_planting(args.declaration, args.field, args.crop,
                                       args.applicant, args.season)
        elif c == "submit-rx":
            emit({"rx_id": svc.submit_prescription(load_json(args.json), args.actor)})
            return 0
        elif c == "approve-rx":
            emit({"snapshot_hash": svc.approve_prescription(args.rx, args.reviewer, args.at)})
            return 0
        elif c == "create-task":
            svc.create_task(args.task, args.rx, args.at, parent_task_id=args.parent)
        elif c == "load-tank":
            svc.load_tank(args.tank, args.batch, args.task, args.at, actor=args.actor)
        elif c == "weather":
            emit(svc.observe_weather(args.task, args.at, args.wind, args.rain))
            return 0
        elif c == "takeoff":
            svc.takeoff(args.task, args.leg, args.at, actor=args.actor)
        elif c == "decide":
            svc.record_recall_decision(args.task, args.leg, args.at, args.decision,
                                       args.by, args.note)
        elif c == "land":
            svc.land(args.task, args.leg, args.at)
        elif c == "sync":
            emit(svc.sync_track_points(args.task, args.leg, args.batch,
                                       load_json(args.json)))
            return 0
        elif c == "analyze":
            emit(svc.analyze(args.task, args.leg))
            return 0
        elif c == "seal":
            emit(svc.seal_conclusion(args.task, args.by, args.at))
            return 0
        elif c == "addendum":
            emit(svc.record_addendum(args.task, args.by, args.at))
            return 0
        elif c == "complaint":
            svc.file_complaint(args.id, args.task, args.at, args.complainant,
                               args.allegation)
        elif c == "trace":
            emit(svc.complaint_trace(args.complaint))
            return 0
        elif c == "export":
            emit(svc.export_summary(args.task))
            return 0
        elif c == "verify":
            emit(svc.verify())
            return 0
        emit({"ok": True, "cmd": c})
        return 0
    except Exception as exc:  # 业务拒绝以非零码退出，错误信息上屏
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
