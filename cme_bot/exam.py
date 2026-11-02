"""考试模块。

平台有几处「反直觉」的地方，代码里的三处关键处理都来自实际踩坑：

1. 题序会变：每次进考试，题目顺序和 qall 里记录的不一样。
   → 一律按「题干 + 选项文本」定位，绝不按位置。

2. 不同题目的选项文本可能完全一样（例如 5 道题选项都是
   「原粒细胞/早幼粒细胞/中性中幼粒细胞…」）。按文本全局搜索会点到第一题，
   后面的题空着，提交被拦、结果页空白。
   → 用 input.name 把单选框分组，向上找到「只含本组按钮」的容器，
     在该容器内定位选项；提交前校验 checked 数量等于题数。

3. 题干前缀可能撞车（两题都以《全国艾滋病检测技术规范…》开头）。
   → 题干去空白标点后整句匹配，不用前 N 个字。
"""
from __future__ import annotations

import itertools
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from playwright.sync_api import Page

LETTER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

# 在「只含本题单选框」的容器里定位选项，返回点击坐标
JS_PICK = """(a) => {
  const stem = a[0], atxt = a[1], idx = a[2];
  const norm = s => (s || '').replace(/\\s/g, '').replace(/[（）()？?、，,。：:；;【】\\[\\]]/g, '');
  const strip = e => ((e ? e.innerText : '') || '').replace(/^[A-E][、.．]\\s*/, '').trim();
  const ins = [...document.querySelectorAll('input[type=radio]')];
  const key = norm(stem);
  const groups = {};
  for (const inp of ins) { const n = inp.name || ('__' + Math.random()); (groups[n] = groups[n] || []).push(inp); }

  let target = null;
  for (const n in groups) {
    const g = groups[n];
    let e = g[0].parentElement, prev = g[0].parentElement;
    while (e && e !== document.body) {
      if (e.querySelectorAll('input[type=radio]').length > g.length) break;
      prev = e; e = e.parentElement;
    }
    if (prev && norm(prev.innerText).includes(key)) { target = prev; break; }
  }
  if (!target) {   // 题干对不上时，退化为「哪个组里有这个选项文本」
    for (const n in groups) {
      const g = groups[n];
      if (g.some(x => strip(x.parentElement) === atxt)) { target = g[0].parentElement; break; }
    }
  }
  const scope = target || document.body;
  const cands = [...scope.querySelectorAll('input[type=radio]')];
  let hit = cands.find(x => strip(x.parentElement) === atxt);
  if (!hit) hit = cands.find(x => strip(x.closest('label') || x.parentElement) === atxt);
  if (!hit) hit = cands.find(x => { const t = strip(x.parentElement); return t && (t.includes(atxt) || atxt.includes(t)); });
  if (!hit && idx < cands.length) hit = cands[idx];        // 文本对不上就按序号兜底
  if (!hit) return null;

  let lbl = hit.closest('label');
  if (!lbl) { let p = hit.parentElement; while (p && p.tagName !== 'LABEL' && p !== document.body) p = p.parentElement; lbl = p; }
  const el = lbl || hit;
  el.scrollIntoView({block: 'center'});
  const r = el.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}"""

JS_QUESTIONS = """() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  for (const box of document.querySelectorAll('.question, .timu, .exam-item, li.clearfix')) {
    const radios = [...box.querySelectorAll('input[type=radio]')];
    if (radios.length < 2) continue;
    const lines = norm(box.innerText).split(/\\s(?=[A-E]、)/);
    const stem = lines[0] || '';
    const opts = radios.map((r, i) => ({
      k: 'ABCDE'[i],
      v: ((r.parentElement && r.parentElement.innerText) || '').replace(/^[A-E][、.．]\\s*/, '').trim(),
    }));
    if (stem && opts.length) out.push({stem, opts});
  }
  return out;
}"""

JS_CHECKED = "() => [...document.querySelectorAll('input[type=radio]')].filter(r => r.checked).length"
JS_SUBMIT = "() => { window.confirm = () => true; const b = document.querySelector('#btn_submit'); if (!b) return 'noBtn'; b.click(); return 'ok'; }"
JS_RESULT = """() => {
  const t = document.body.innerText;
  if (/未通过|不合格|不及格/.test(t)) return '未通过';
  if (/考试通过|通过|合格|及格/.test(t)) return '通过';
  return '';
}"""

FOOTER = re.compile(r"\s*北京华医网|\s*京ICP|京公网安备.*$", re.S)


def clean(text: str) -> str:
    return FOOTER.split(text or "")[0].strip()


def _delay(cfg: Dict[str, Any]) -> float:
    lo, hi = cfg.get("action_delay", [0.25, 0.6])
    return random.uniform(float(lo), float(hi))


def collect_questions(page: Page, session, cwid: str, name: str = "") -> Optional[Dict[str, Any]]:
    """只读采集本节考试的题目与选项（不改任何状态）。"""
    page.goto(f"{session.base}/pages/exam.aspx?cwid={cwid}", wait_until="domcontentloaded", timeout=45000)
    time.sleep(2.5)
    for _ in range(4):
        if session.solve_inline_captcha(page):
            continue
        break
    qs = page.evaluate(JS_QUESTIONS)
    if not qs:  # 兜底：按 name 分组重建
        qs = page.evaluate("""() => {
          const groups = {};
          [...document.querySelectorAll('input[type=radio]')].forEach(r => {
            const n = r.name || Math.random();
            (groups[n] = groups[n] || []).push(r);
          });
          return Object.values(groups).map((g, qi) => {
            let e = g[0].parentElement, prev = e;
            while (e && e !== document.body) {
              if (e.querySelectorAll('input[type=radio]').length > g.length) break;
              prev = e; e = e.parentElement;
            }
            const first = ((g[0].parentElement && g[0].parentElement.innerText) || '');
            return {
              stem: 'Q' + (qi + 1),
              opts: g.map((r, i) => ({k: 'ABCDE'[i],
                v: ((r.parentElement && r.parentElement.innerText) || '').replace(/^[A-E][、.．]\\s*/, '').trim()})),
              _block: (prev ? prev.innerText : '').slice(0, 40), _first: first.slice(0, 20),
            };
          });
        }""")
    if not qs:
        return None
    for q in qs:
        q.pop("_block", None)
        q.pop("_first", None)
    return {"cwid": cwid, "name": name, "qs": qs}


def submit(page: Page, session, cwid: str, qs: Dict[str, Any], letters: Sequence[str]) -> str:
    """按候选字母作答并交卷，返回 '通过' / '未通过' / '?'（页面异常）。"""
    page.goto(f"{session.base}/pages/exam.aspx?cwid={cwid}", wait_until="domcontentloaded", timeout=45000)
    time.sleep(1.5)

    for _ in range(4):  # 反作弊验证码可能延迟弹出
        if session.solve_inline_captcha(page):
            continue
        break

    n = len(qs["qs"])
    for j, lt in enumerate(letters):
        if j >= n:
            break
        opts = qs["qs"][j]["opts"]
        oi = min(LETTER.get(lt, 0), len(opts) - 1)
        atxt = clean(opts[oi]["v"])
        rect = page.evaluate(JS_PICK, [clean(qs["qs"][j]["stem"]), atxt, oi])
        if not rect:
            continue
        page.mouse.move(rect["x"], rect["y"])
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.click(rect["x"], rect["y"])
        time.sleep(_delay(session.cfg))

    checked = page.evaluate(JS_CHECKED)
    if checked < n:  # 有题没点上，再补一轮（同题块内按序号兜底）
        for j, lt in enumerate(letters):
            if j >= n:
                break
            opts = qs["qs"][j]["opts"]
            oi = min(LETTER.get(lt, 0), len(opts) - 1)
            rect = page.evaluate(JS_PICK, [clean(qs["qs"][j]["stem"]), clean(opts[oi]["v"]), oi])
            if rect:
                page.mouse.click(rect["x"], rect["y"])
                time.sleep(_delay(session.cfg))
        checked = page.evaluate(JS_CHECKED)

    page.evaluate(JS_SUBMIT)
    time.sleep(5)
    for _ in range(3):  # 提交后可能再次弹验证码
        if session.solve_inline_captcha(page):
            continue
        break
    time.sleep(1)

    flag = page.evaluate(JS_RESULT)
    if not flag:
        try:
            tail = page.inner_text("body")[:100].replace("\n", "|")
        except Exception:
            tail = "unreadable"
        return f"?{checked}?{tail}"
    return flag


MAX_COMBOS = 400  # 单门课的组合数上限：超过这个数，说明候选集没收敛，先回去看题


def _pools(candidates: Sequence[Sequence[str]], qs: Dict[str, Any]) -> List[List[str]]:
    groups = []
    for j, q in enumerate(qs["qs"]):
        c = list(dict.fromkeys(candidates[j])) if j < len(candidates) and candidates[j] else ["A"]
        c = [x for x in c if LETTER.get(x, 9) < len(q["opts"])] or ["A"]
        groups.append(c)
    return groups


def space_size(candidates: Sequence[Sequence[str]], qs: Dict[str, Any]) -> int:
    """候选集的组合总数，交卷前先看一眼，别盲跑。"""
    n = 1
    for g in _pools(candidates, qs):
        n *= len(g)
    return n


def solve(session, page: Page, cwid: str, qs: Dict[str, Any],
          candidates: Sequence[Sequence[str]], on_result=None) -> Optional[List[str]]:
    """按「每题候选集」做笛卡尔积穷举，命中即返回答案文本。

    candidates 形如 [["C"], ["A","B"], ["B","C"], ["A"], ["C"]]
    每题只列高概率选项，组合数就会很小（通常几十次以内）。
    """
    groups = _pools(candidates, qs)
    combos = list(itertools.product(*groups))
    for k, combo in enumerate(combos, 1):
        flag = submit(page, session, cwid, qs, combo)
        if on_result:
            on_result(k, len(combos), "".join(combo), flag)
        if flag.startswith("?"):
            return None
        if flag == "通过":
            return [clean(qs["qs"][j]["opts"][min(LETTER[combo[j]], len(qs["qs"][j]["opts"]) - 1)]["v"])
                    for j in range(len(qs["qs"]))]
    return None
