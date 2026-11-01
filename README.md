# huayi-cme-bot

华医网（cme40.91huayi.com）继续医学教育课程的**学习进度自动化**脚本：自动登录、批量播放课程视频到真正结束、采集考试题目、按答案库交卷、并在服务端核验真实完成状态。

> 面向"自己给自己刷学分"的个人使用场景。不提供答案、不绕过付费课程，也不做任何破坏平台数据的行为。

---

## 它能做什么

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 自动登录（含 5 位数字验证码 OCR） | ✅ | `ddddocr` 本地识别，失败自动刷新重试 |
| 批量看课 | ✅ | 逐节播放到 `ended`，支持断点续播、卡住自动重载、崩溃可重入 |
| 采集考试题目 | ✅ | 只读，把题干和选项落到 `data/questions.json` |
| 按答案库交卷 | ✅ | 题干→答案文本→选项字母，逐题点击 |
| 候选集暴力求解 | ✅ | 每题给几个候选，笛卡尔积穷举，命中即存答案 |
| 服务端状态核验 | ✅ | 逐项目读 `course.aspx`，用真实百分比戳穿"假完成" |
| 付费/需激活的课程 | ❌ | 需要用户自己在网页上报名或用卡激活 |

**关于考试**：脚本只负责"点对选项"。答案需要你自己维护在 `data/answers.json` 里（`{cwid: [答案文本, ...]}`），或者给候选集让它穷举。脚本不会猜医学题，也不联网搜答案。

---

## 三条必须知道的坑

这三条是踩了很久才摸清的，直接影响成败，写在最前面：

### 1. 视频"播到 99%"不算完成，必须真的 `ended`

平台只在视频触发 `ended` 且完成信号上报之后，才把课程状态从「学习中:99%」翻成「待考试」。任何"看到 `t >= duration - 2` 就切下一节"的优化都会产生**假完成**——本地记录好看，服务端还是 99%。

代码里对应 `watch.py` 的处理：等 `video.ended` → 再等 `ended_buffer_sec`（默认 18s）让上报落库 → 才写 `watched`。

### 2. 页面必须"可见"，否则进度不上报

浏览器被最小化、切到后台、或 `document.hidden === true` 时，播放器的进度就不再上报到服务端，本地却在走。所以注入的 stealth 脚本会强制：

```js
Object.defineProperty(document, 'hidden', {get: () => false});
Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
```

同时隐藏 `navigator.webdriver`。没有这一步，看课会"看起来很顺但一门都过不了"。

### 3. 考试选项跨题重名，全局文本搜索会点错题

同一套卷子里，不同题目出现完全相同的选项文本是常态（比如多个题都有"以上都是"）。如果按选项文本全局找元素，所有点击都会落到第一题上，后面的题空着，提交直接报"页面异常"。

正确做法是**按 `input[name]` 分组**：每题的 5 个 radio 共享同一个 `name="radio_<guid>"`，先分组、再用归一化题干文本确定分组属于哪一题，最后只在该分组内点。

```python
groups = {}
for inp in inputs:
    groups.setdefault(inp.name, []).append(inp)   # 一题一组
```

另外，题干也可能前缀相同（两个题都以《全国艾滋病检测技术规范……》开头），所以匹配要用**完整归一化题干**，不能 `startswith` 前 10 个字。

提交前必须校验已选数 == 题目数，否则重来一遍。

---

## 环境要求

- Python 3.9+
- Windows / macOS / Linux 均可（开发环境 Windows 11 + Chrome）
- 本机已安装 Chrome（脚本用 `channel="chrome"` 启动持久化上下文）

## 安装

```bash
git clone https://github.com/<your-name>/huayi-cme-bot.git
cd huayi-cme-bot

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium      # 保险起见
```

`ddddocr` 首次运行会自动下载模型（约 10MB）。

## 配置

```bash
cp config.example.json config.json
```

```json
{
  "username": "身份证号或登录账号",
  "password": "密码",
  "base_url": "https://cme40.91huayi.com",
  "headless": false,
  "profile_dir": ".chrome-profile",
  "state_file": "data/state.json",
  "answers_file": "data/answers.json",
  "questions_file": "data/questions.json",
  "log_file": "data/watch.log",
  "progress_interval_sec": 240,
  "ended_buffer_sec": 18,
  "action_delay": [0.25, 0.6]
}
```

也可以用环境变量覆盖账号，避免把密码写进文件：

```bash
export CME_USERNAME=你的账号
export CME_PASSWORD=你的密码
```

`config.json`、`.chrome-profile/`、`data/` 已在 `.gitignore` 中，不会被提交。

## 使用

按顺序跑，每步都可以中断再继续。

```bash
# 1. 登录一次，确认账号密码和验证码链路是通的
python -m cme_bot login

# 2. 抓取「学习中」的项目和课程，写入 data/courses.json
python -m cme_bot catalog

# 3. 挂机看课（可以一直跑，断了重跑会跳过已完成的）
python -m cme_bot watch

# 4. 只读采集待考课程的题目
python -m cme_bot questions

# 5. 交卷：优先用答案库，没有答案的走候选集穷举
python -m cme_bot exam --answers data/answers.json --cands data/candidates.json

# 6. 服务端核验真实状态（唯一可信的完成度来源）
python -m cme_bot verify
```

### 看课的时长预期

平台按**真实观看时长**计分，且是 1 倍速。34 分钟的课就要 34 分钟，60 分钟的课就要 1 小时。这就是为什么 `watch` 适合挂机，而不是"跑一下就好"。

一个批次 60 多门课、60 多小时是常态。建议：

```bash
# Windows 后台跑，日志单独留一份
python -u -m cme_bot watch >> data/run.log 2>&1
```

### 答案库格式

`data/answers.json`：

```json
{
  "e451e3a2-....-....": ["答案文本1", "答案文本2", "答案文本3"],
  "88064c15-....-....": ["...", "...", "...", "...", "..."]
}
```

按题目顺序给**选项文本**（不是字母），脚本会自动转成字母。用文本而不是字母，是因为题库顺序会变，文本更稳。

### 候选集格式

不清楚答案、但能圈定每题的 2–3 个可能选项时，用候选集：

`data/candidates.json`：

```json
{
  "cwid": [["A","C"], ["B"], ["A","B","D"], ["C"], ["D"]]
}
```

脚本跑笛卡尔积（这里是 2×1×3×1×1 = 6 种组合），命中「通过」就把当次选择写回 `answers.json`，下次直接用。别把候选集开太大，组合爆炸会很慢。

## 数据文件

| 文件 | 内容 |
| --- | --- |
| `data/courses.json` | `catalog` 抓下来的项目 + 课程清单 |
| `data/state.json` | 每门课的 `watched` / `exam_done` 标记，断点续跑靠它 |
| `data/questions.json` | 采集到的题干与选项 |
| `data/answers.json` | 已验证的答案文本 |
| `data/verify.json` | `verify` 的服务端核验明细 |
| `data/watch.log` | 看课日志，每 4 分钟一行进度 |

`state.json` 是唯一进度来源，删了就得从头判断；备份它比备份别的都重要。

## 目录结构

```
cme_bot/
  config.py     配置合并（默认值 + config.json + 环境变量）
  session.py    Playwright 会话、stealth 注入、验证码 OCR、登录
  watch.py      播放器轮询、状态机、队列调度
  exam.py       题目采集、按 name 分组的选项点击、穷举求解
  catalog.py    项目/课程清单抓取、服务端状态核验
  cli.py        命令行入口
```

## 已知限制

- **付费课程动不了**。显示「试看 3 分钟」或项目页标价（¥18–26）的，必须你自己在网页上完成报名/用卡激活，脚本没有支付能力。
- **互动病例类课程**（`hdbl.91huayi.com`，"查看病例"入口）没有 video 元素，需要人工阅读病例、回答嵌入问题并等待计时，自动化意义不大。
- **会话会掉**。站点会话只有几分钟到几十分钟，脚本内置"cookie 优先 + 验证码重登"兜底，但如果账号被风控锁住，需要人工介入一次。
- **同一个 `.chrome-profile` 同一时刻只能一个进程用**。并行跑会互相抢锁，启动报 `TargetClosedError`。崩了先清 `SingletonLock` 和残留 `chrome.exe`。
- **不保证 100% 通过**。答案错就是错，脚本不会帮你判断医学内容对错；`verify` 才是唯一的判断依据。

## 免责声明

本项目仅供个人学习与技术研究，用于自动化处理自己的继续教育学习进度。

- 请遵守华医网的用户协议与所在单位的继续教育管理规定。
- 请勿用于代他人刷学分、批量注册、售卖学分等行为。
- 使用本项目产生的一切后果（包括但不限于账号被封、学分作废、执业资格相关问题）由使用者自行承担。
- 作者不对平台的任何接口变更、反爬升级负责，也不提供任何形式的"包过"承诺。

如果平台方认为本项目不妥，请联系删除。

## License

MIT
