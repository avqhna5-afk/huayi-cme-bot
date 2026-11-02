"""命令行入口。

典型顺序：
    python -m cme_bot catalog          # 抓「学习中」的项目与课程
    python -m cme_bot watch            # 逐节播完（可挂机，支持断点续播）
    python -m cme_bot questions        # 只读采集考试题目
    python -m cme_bot exam             # 按 answers / 候选集交卷
    python -m cme_bot verify           # 服务端核验真实状态
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from . import catalog, exam, watch
from .config import load
from .session import Session

COURSES_FILE = "data/courses.json"


def log_factory(cfg: Dict[str, Any]):
    path = Path(cfg["log_file"])
    path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    return log


def load_courses(cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = Path(cfg["state_file"]).parent / "courses.json"
    if not p.exists():
        raise SystemExit("请先运行: python -m cme_bot catalog")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_login(cfg):
    with Session(cfg) as s:
        page = s.ctx.new_page()
        ok = s.ensure_login(page)
        print("登录成功" if ok else "登录失败")


def cmd_catalog(cfg):
    out: Dict[str, Any] = {"projects": [], "courses": []}
    with Session(cfg) as s:
        page = s.ctx.new_page()
        if not s.ensure_login(page):
            raise SystemExit("登录失败")
        projects = catalog.list_projects(page, s, only_learning=True)
        print(f"学习中项目 {len(projects)} 个")
        for p in projects:
            info = catalog.list_courses(page, s, p["cid"])
            print(f"  {p['name'][:30]} 课程 {len(info['courses'])} 门 考试={'有' if info['has_exam'] else '无'}")
            out["projects"].append({**p, "has_exam": info["has_exam"]})
            for c in info["courses"]:
                out["courses"].append({**c, "cid": p["cid"], "project": p["name"]})
    path = Path(cfg["state_file"]).parent / "courses.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {path}（{len(out['courses'])} 门课）")


def cmd_watch(cfg):
    state = watch.State(cfg["state_file"])
    data = load_courses(cfg)
    courses = [{"cwid": c["cwid"], "name": c["name"], "tot": c.get("tot") or 3600}
               for c in data["courses"] if c.get("cwid")]
    log = log_factory(cfg)
    with Session(cfg) as s:
        page = s.ctx.new_page()
        if not s.ensure_login(page):
            raise SystemExit("登录失败")
        watch.run_queue(s, courses, state, log)


def cmd_questions(cfg):
    state = watch.State(cfg["state_file"])
    data = load_courses(cfg)
    todo = [c for c in data["courses"]
            if c.get("cwid") and state.get(c["cwid"]).get("watched")
            and not state.get(c["cwid"]).get("exam_done")]
    out = {}
    path = Path(cfg["questions_file"])
    with Session(cfg) as s:
        page = s.ctx.new_page()
        if not s.ensure_login(page):
            raise SystemExit("登录失败")
        for i, c in enumerate(todo, 1):
            q = exam.collect_questions(page, s, c["cwid"], c["name"])
            if q:
                out[c["cwid"]] = q
                print(f"[{i}/{len(todo)}] {c['name'][:26]} 采集 {len(q['qs'])} 题", flush=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {path}")


def cmd_exam(cfg, answers_path: str, cands_path: str):
    """answers: {cwid: [答案文本...]}；cands: {cwid: [[候选字母...]...]}"""
    state = watch.State(cfg["state_file"])
    qfile = Path(cfg["questions_file"])
    if not qfile.exists():
        raise SystemExit("请先运行: python -m cme_bot questions")
    bank = json.loads(qfile.read_text(encoding="utf-8"))
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8")) if Path(answers_path).exists() else {}
    cands = json.loads(Path(cands_path).read_text(encoding="utf-8")) if Path(cands_path).exists() else {}

    with Session(cfg) as s:
        page = s.ctx.new_page()
        if not s.ensure_login(page):
            raise SystemExit("登录失败")
        for cwid, q in bank.items():
            if state.get(cwid).get("exam_done"):
                continue
            name = q.get("name", cwid[:8])
            letters = None
            if cwid in answers:      # 已知答案：文本 → 字母
                letters = []
                for j, text in enumerate(answers[cwid]):
                    opts = q["qs"][j]["opts"]
                    hit = "A"
                    for i, o in enumerate(opts):
                        if exam.clean(o["v"]) == exam.clean(text):
                            hit = "ABCDE"[i]
                            break
                    letters.append(hit)
            if letters:
                flag = exam.submit(page, s, cwid, q, letters)
                print(f"{cwid[:8]} {name[:22]} {'已提交'} -> {flag}", flush=True)
                if flag == "通过":
                    state.set(cwid, exam_done=True, last_flag="通过")
                continue
            if cwid not in cands:
                print(f"{cwid[:8]} {name[:22]} 无答案无候选集，跳过", flush=True)
                continue

            def on_result(k, n, combo, flag, _c=cwid):
                print(f"  {_c[:8]} 试{k}/{n} {combo} -> {flag}", flush=True)

            space = exam.space_size(cands[cwid], q)
            if space > exam.MAX_COMBOS:
                print(f"{cwid[:8]} {name[:22]} 候选组合 {space} 种，超过上限 "
                      f"{exam.MAX_COMBOS}，先收窄候选集（见 README 第 6 条）", flush=True)
                continue
            print(f"{cwid[:8]} {name[:22]} 候选空间 {space} 种，开始穷举", flush=True)

            found = exam.solve(s, page, cwid, q, cands[cwid], on_result)
            if found:
                answers[cwid] = found
                Path(answers_path).write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
                state.set(cwid, exam_done=True, last_flag="通过")
                print(f">>> {cwid[:8]} 通过，答案已保存", flush=True)
            else:
                print(f"<<< {cwid[:8]} 候选集未命中", flush=True)


def cmd_verify(cfg):
    with Session(cfg) as s:
        page = s.ctx.new_page()
        if not s.ensure_login(page):
            raise SystemExit("登录失败")
        res = catalog.verify(page, s)
    stat: Dict[str, int] = {}
    for cid, courses in res.items():
        for name, st in courses.items():
            stat[st] = stat.get(st, 0) + 1
    print(json.dumps(stat, ensure_ascii=False))
    out = Path(cfg["state_file"]).parent / "verify.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细已写入 {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cme_bot", description="华医网继续教育自动化")
    ap.add_argument("command", choices=["login", "catalog", "watch", "questions", "exam", "verify"])
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--answers", default="data/answers.json")
    ap.add_argument("--cands", default="data/candidates.json")
    args = ap.parse_args(argv)

    cfg = load(args.config)
    if args.command == "login":
        cmd_login(cfg)
    elif args.command == "catalog":
        cmd_catalog(cfg)
    elif args.command == "watch":
        cmd_watch(cfg)
    elif args.command == "questions":
        cmd_questions(cfg)
    elif args.command == "exam":
        cmd_exam(cfg, args.answers, args.cands)
    elif args.command == "verify":
        cmd_verify(cfg)


if __name__ == "__main__":
    main(sys.argv[1:])
