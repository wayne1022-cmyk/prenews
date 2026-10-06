"""
共用設定與工具：路徑、時區、環境變數、JSON 讀寫、logging。
三個階段都 import 這個檔案，設定只維護一份。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = BASE_DIR / "debug"

STAGE1_FILE = DATA_DIR / "premarket_result.json"
STAGE2_FILE = DATA_DIR / "premarket_ai_result.json"

# 各階段共用的狀態值
SUCCESS = "success"
PARTIAL = "partial_success"
FAILED = "failed"
NO_DATA = "no_data"      # 例如休市日、今天的盤前尚未發布
RUNNING = "running"      # 第二階段逐篇存檔時的中間狀態


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def today_tw() -> str:
    """台灣今天日期 YYYYMMDD。GitHub runner 是 UTC，日期判斷一律用這個。"""
    return now_tw().strftime("%Y%m%d")


def format_date(yyyymmdd: str | None) -> str:
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd or "未知日期"


def setup_logging(name: str) -> logging.Logger:
    """log 顯示台灣時間，方便在 Actions log 判斷卡在哪一步。"""
    # 設為類別屬性時會被當成方法呼叫，用 staticmethod 包起來
    logging.Formatter.converter = staticmethod(
        lambda secs: datetime.fromtimestamp(secs, TW_TZ).timetuple()
    )
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    return logging.getLogger(name)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少環境變數 {name}（本機請 export，GitHub 請設定在 Secrets）")
    return value


def env_str(name: str, default: str) -> str:
    # GitHub 未設定的 vars 會是空字串，所以用 or 而不是 get 的預設值
    return os.environ.get(name, "").strip() or default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def write_json(path: Path, data: dict[str, Any]) -> None:
    """先寫暫存檔再原子性取代：寫到一半當掉也不會留下損壞的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "generated_at": now_tw().isoformat(timespec="seconds")}
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None
