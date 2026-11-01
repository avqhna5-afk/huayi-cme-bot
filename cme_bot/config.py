"""配置加载：优先 config.json，其次环境变量，最后默认值。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "base_url": "https://cme40.91huayi.com",
    "headless": False,
    "profile_dir": ".chrome-profile",
    "state_file": "data/state.json",
    "answers_file": "data/answers.json",
    "questions_file": "data/questions.json",
    "log_file": "data/watch.log",
    "progress_interval_sec": 240,
    "ended_buffer_sec": 18,
    "action_delay": [0.25, 0.6],
}


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def load(path: str = "config.json") -> Dict[str, Any]:
    cfg: Dict[str, Any] = dict(DEFAULTS)

    f = _resolve(path)
    if f.exists():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))

    # 环境变量优先，方便 CI / 不落盘密码
    cfg["username"] = os.environ.get("CME_USERNAME", cfg.get("username", ""))
    cfg["password"] = os.environ.get("CME_PASSWORD", cfg.get("password", ""))
    if os.environ.get("CME_BASE_URL"):
        cfg["base_url"] = os.environ["CME_BASE_URL"]

    if not cfg["username"] or not cfg["password"]:
        raise SystemExit(
            "缺少账号密码：请复制 config.example.json 为 config.json 填写，"
            "或设置环境变量 CME_USERNAME / CME_PASSWORD。"
        )

    for key in ("profile_dir", "state_file", "answers_file", "questions_file", "log_file"):
        cfg[key] = str(_resolve(cfg[key]))

    Path(cfg["state_file"]).parent.mkdir(parents=True, exist_ok=True)
    return cfg
