"""课程清单与服务端核验。

- 从「学习记录」发现仍在学习的项目（含选修），再逐个项目页抓课程与时长。
- verify() 直接读项目页每门课的状态标签，这是唯一可信的进度来源：
  考试结果页在反作弊拦截时会返回空白，只看它会误判「已通过」。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

JS_PROJECTS = """() => {
  const out = [];
  for (const tr of document.querySelectorAll('tr')) {
    const tds = [...tr.querySelectorAll('td')];
    if (tds.length < 6) continue;
    const a = tr.querySelector('a[href*="course.aspx?cid="]');
    if (!a) continue;
    const t = tds.map(td => td.innerText.trim());
    out.push({name: t[0], code: t[1] || '', credit: t[2] || '', state: t[3] || '',
              progress: t[5] || '', cid: (a.getAttribute('href').split('cid=')[1] || '')});
  }
  return out;
}"""

JS_COURSES = """() => {
  const rows = [...document.querySelectorAll('div.cw-progress-row')];
  return rows.map(r => {
    const up = r.closest('div.course') || r.parentElement;
    const a = up ? up.querySelector('a') : null;
    const attrs = {};
    for (const at of r.attributes) attrs[at.name] = at.value;
    let tot = 0;
    for (const k in attrs) {
      if (/tot|duration|time/i.test(k)) {
        const v = parseFloat(attrs[k]);
        if (!isNaN(v) && v > 60) tot = Math.round(v);
      }
    }
    return {name: (a ? a.innerText : '').trim(), state: r.getAttribute('data-studystate'),
            cwid: r.getAttribute('data-cwrid'), tot};
  });
}"""

DONE_STATES = ("已完成", "待考试")


def list_projects(page: Page, session, only_learning: bool = True) -> List[Dict[str, Any]]:
    page.goto(f"{session.base}/pages/study_info_list.aspx", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3.5)
    rows = page.evaluate(JS_PROJECTS)
    if only_learning:
        rows = [r for r in rows if "学习中" in (r.get("state") or "") and r.get("cid")]
    return rows


def list_courses(page: Page, session, cid: str) -> Dict[str, Any]:
    page.goto(f"{session.base}/pages/course.aspx?cid={cid}", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)
    courses = page.evaluate(JS_COURSES)
    body = page.inner_text("body")
    return {
        "cid": cid,
        "has_exam": "考试" in body,
        "courses": [c for c in courses if c.get("cwid")],
    }


def verify(page: Page, session, project_ids: Optional[List[str]] = None) -> Dict[str, Dict[str, str]]:
    """逐项目抓每门课的状态标签。返回 {cid: {课程名: 状态}}"""
    result: Dict[str, Dict[str, str]] = {}
    ids = project_ids or [r["cid"] for r in list_projects(page, session, only_learning=False)]
    for cid in ids:
        try:
            page.goto(f"{session.base}/pages/course.aspx?cid={cid}", wait_until="domcontentloaded", timeout=45000)
            time.sleep(2.5)
            text = page.inner_text("body")
            info = list_courses(page, session, cid)
            status: Dict[str, str] = {}
            for c in info["courses"]:
                name = c["name"]
                i = text.find(name)
                win = text[i:i + 140] if i >= 0 else ""
                m = re.search(r"(\d+)\s*%", win)
                if m:
                    status[name] = f"学习中:{m.group(1)}%"
                else:
                    for s in ("待考试", "已完成", "未学习", "未开始"):
                        if s in win:
                            status[name] = s
                            break
                    else:
                        status[name] = c.get("state") or "?"
            result[cid] = status
        except Exception:
            result[cid] = {}
    return result
