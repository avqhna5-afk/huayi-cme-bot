"""看课：按队列逐节课播放到真正 ended，服务端才会把课程记为「待考试」。

最容易踩的两个坑：
1. 提前切走 = 假完成。平台只在视频真的 ended（并且完成信号上报）后才记
   「待考试」；停在 d-2 秒就切下一节，服务端只会留下 82~99% 的学习中。
   所以这里必须等 ended，再多等 ended_buffer_sec 秒。
2. 进度不动 = 标签页被判后台。必须伪装 visibilityState（见 session.STEALTH_JS），
   否则播放器不上报时长。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

JS_TICK = """() => {
  const r = {};
  const s = document.querySelector('.btn_sign');          // 随堂签到
  if (s) { try { s.click(); r.sign = 1; } catch (e) {} }
  const ok = [...document.querySelectorAll('button,a,span')].find(e => {
    const t = (e.innerText || '').trim();
    return (t === '确定' || t === '知道了') && e.offsetParent !== null;   // 各类提示弹窗
  });
  if (ok) { try { ok.click(); r.ok = 1; } catch (e) {} }
  r.url = location.href;
  const v = document.querySelector('video');
  if (!v) return r;
  try { v.muted = true; } catch (e) {}
  if (v.paused && !v.ended) { try { v.play(); r.play = 1; } catch (e) {} }
  r.t = v.currentTime || 0;
  r.d = (v.duration && !isNaN(v.duration)) ? v.duration : 0;
  r.ended = v.ended;
  r.wd = navigator.webdriver;
  return r;
}"""


class State:
    """state.json：cwid -> {watched, exam_done, name, fails}"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, cwid: str) -> Dict[str, Any]:
        return self.data.get(cwid, {})

    def set(self, cwid: str, **kw) -> None:
        self.data.setdefault(cwid, {}).update(kw)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


class WatchResult:
    def __init__(self, ok: bool, reason: str, seconds: float = 0.0):
        self.ok = ok
        self.reason = reason
        self.seconds = seconds


def tick(page: Page, tries: int = 4) -> Dict[str, Any]:
    for i in range(tries):
        try:
            return page.evaluate(JS_TICK)
        except Exception as e:
            if i + 1 < tries and any(k in str(e) for k in ("Execution context", "Target page", "navigation")):
                time.sleep(3)
                continue
            return {}
    return {}


def watch_one(session, page: Page, cwid: str, name: str, total_sec: int, log) -> WatchResult:
    """播完一节。返回是否真正 ended。"""
    base = session.base
    page.goto(f"{base}/course_ware/course_ware.aspx?cwid={cwid}", wait_until="domcontentloaded", timeout=45000)
    t0 = time.time()
    last_report = 0.0
    last_t = 0.0
    stall = 0
    interval = int(session.cfg.get("progress_interval_sec", 240))
    buffer_sec = int(session.cfg.get("ended_buffer_sec", 18))
    timeout = max(total_sec, 600) + 1800

    while True:
        if os.path.exists(Path(session.cfg["state_file"]).parent / "stop.txt"):
            return WatchResult(False, "stopped", time.time() - t0)
        if time.time() - t0 > timeout:
            log(f"TIMEOUT {cwid[:8]} {name[:20]}")
            return WatchResult(False, "timeout", time.time() - t0)

        r = tick(page)
        if "noplay" in r.get("url", ""):
            log(f"NOPLAY {cwid[:8]}（名额/权限问题，稍后重试）")
            return WatchResult(False, "noplay", time.time() - t0)

        if r.get("ended"):
            log(f"END {cwid[:8]} t={r.get('t', 0):.0f}/{r.get('d', 0):.0f} wd={r.get('wd')}")
            try:  # 有些播放器 ended 后 currentTime 不落满，补一次 play 让完成信号发出
                page.evaluate("() => { const v=document.querySelector('video'); if(v && !v.ended){ try{v.play();}catch(e){} } }")
            except Exception:
                pass
            time.sleep(buffer_sec)
            return WatchResult(True, "ended", time.time() - t0)

        # 接近结尾但还没 ended：继续轮询，别切走
        if r.get("d") and r.get("t", 0) >= r["d"] - 2:
            time.sleep(3)
            continue

        if abs(r.get("t", 0) - last_t) < 0.4:
            stall += 1
        else:
            stall = 0
        last_t = r.get("t", 0)

        if stall >= 40:  # 卡住 80 秒 → 重载播放器（断点续播会接上）
            log(f"STALL {cwid[:8]} t={last_t:.0f} 重载")
            page.goto(f"{base}/course_ware/course_ware.aspx?cwid={cwid}", wait_until="domcontentloaded", timeout=45000)
            stall = 0

        if time.time() - last_report > interval:
            last_report = time.time()
            log(f"  {cwid[:8]} {last_t / 60:.0f}/{(r.get('d') or 0) / 60:.0f} min wd={r.get('wd')}")
        time.sleep(2)


def run_queue(session, courses: List[Dict[str, Any]], state: State, log, exam_hook=None) -> None:
    """courses: [{cwid, name, tot}]，按顺序播；失败最多重试 3 次。"""
    queue = [
        {**c, "_fail": int(state.get(c["cwid"]).get("fails") or 0)}
        for c in courses
        if not state.get(c["cwid"]).get("watched")
    ]
    if not queue:
        log("没有待看课程")
        return

    log(f"queue = {len(queue)} 门, 总时长 {sum(x['tot'] for x in queue) / 3600:.1f} 小时")
    page = session.ctx.new_page()
    while queue:
        item = queue.pop(0)
        cwid, name = item["cwid"], item["name"]
        log(f"OPEN {cwid[:8]} {name[:24]} ({item['tot'] // 60} min)")
        try:
            res = watch_one(session, page, cwid, name, item["tot"], log)
        except Exception as e:
            log(f"ERR {cwid[:8]} {str(e)[:100]}")
            res = WatchResult(False, "error")

        if not res.ok:
            if res.reason == "stopped":
                log("检测到 stop.txt，退出")
                return
            item["_fail"] += 1
            state.set(cwid, watched=False, fails=item["_fail"], name=name)
            if item["_fail"] < 3:
                queue.append(item)
            continue

        state.set(cwid, watched=True, name=name, seconds=round(res.seconds))
        log(f"WATCHED {cwid[:8]} {name[:20]}")
        if exam_hook:
            try:
                exam_hook(cwid, name)
            except Exception as e:
                log(f"exam hook err {str(e)[:80]}")
    try:
        page.close()
    except Exception:
        pass
