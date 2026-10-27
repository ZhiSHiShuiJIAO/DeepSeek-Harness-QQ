# -*- coding: utf-8 -*-
"""提醒 / 待办：把「明天八点叫我」变成真的定时任务。

设计取舍：
  - 只认明确时间（明天/今天/后天 + 点/时/分、HH:MM、N 分钟/小时后），模糊的「待会儿」不接
  - 存 JSON，重启不丢
  - 独立线程每 15 秒扫一次，到点就回调，不依赖模型记性
"""

import json
import os
import re
import threading
import time
import uuid

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s: str) -> int:
    """把「八」「十二」「二十三」这类中文数字转成整数。"""
    s = s.strip()
    if not s:
        raise ValueError("empty")
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        head, _, tail = s.partition("十")
        return (CN_NUM.get(head, 1) if head else 1) * 10 + (CN_NUM.get(tail, 0) if tail else 0)
    total = 0
    for ch in s:
        total = total * 10 + CN_NUM.get(ch, 0)
    return total


def parse_when(text: str, now: float | None = None):
    """从一句话里认出时间戳；认不出返回 None。"""
    now = now if now is not None else time.time()
    t = time.localtime(now)
    base_day = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))

    # 相对时间：半小时后 / 20 分钟后 / 2 小时后
    m = re.search(r"(\d+|[一二两三四五六七八九十]+)\s*个?\s*(半)?\s*(分钟|分|小时|钟头)\s*(?:以)?后", text)
    if m:
        n = _cn_int(m.group(1))
        if m.group(3) in ("小时", "钟头"):
            secs = n * 3600
        else:
            secs = n * 60
        if m.group(2):
            secs += (1800 if m.group(3) in ("小时", "钟头") else 30)
        return now + secs

    # 日期偏移
    explicit_today = any(k in text for k in ("今天", "今晚", "今早", "今夜"))
    if "大后天" in text:
        day_off = 3
    elif "后天" in text:
        day_off = 2
    elif "明天" in text or "明日" in text:
        day_off = 1
    elif "今天" in text or "今晚" in text or "今早" in text:
        day_off = 0
    else:
        day_off = 0

    # 钟点：八点 / 8点 / 八点半 / 8:30 / 早上七点
    hour = minute = None
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2}|[一二两三四五六七八九十]+)\s*[点时]\s*(半|(\d{1,2}|[一二两三四五六七八九十]+)\s*分)?", text)
        if m:
            hour = _cn_int(m.group(1))
            if m.group(2) == "半":
                minute = 30
            elif m.group(3):
                minute = _cn_int(m.group(3))
            else:
                minute = 0

    if hour is None:
        return None
    if minute is None:
        minute = 0

    if any(k in text for k in ("今晚", "今夜", "晚上", "下午", "傍晚")) and hour < 12:
        hour += 12
    if any(k in text for k in ("凌晨", "半夜")) and hour == 12:
        hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    ts = base_day + day_off * 86400 + hour * 3600 + minute * 60
    if day_off == 0 and not explicit_today and ts <= now:
        # 说的时间已经过了，按明天算
        ts += 86400
    return ts


def strip_time_words(text: str) -> str:
    """把时间部分从正文里削掉，剩下的当提醒内容。"""
    cleaned = re.sub(r"(\d{1,2}\s*[:：]\s*\d{2})", " ", text)
    cleaned = re.sub(r"(明天|明日|明早|明晚|明晨|今日|今天|后天|大后天|今晚|今早|早上|上午|中午|下午|晚上|傍晚|凌晨|半夜)", " ", cleaned)
    cleaned = re.sub(r"(\d{1,2}|[一二两三四五六七八九十]+)\s*[点时](半|\s*(\d{1,2}|[一二两三四五六七八九十]+)\s*分)?", " ", cleaned)
    cleaned = re.sub(r"(\d+|[一二两三四五六七八九十]+)\s*个?\s*(半)?\s*(分钟|分|小时|钟头)\s*(?:以)?后", " ", cleaned)
    cleaned = cleaned.replace("叫我", " ").replace("提醒我", " ").replace("提醒", " ")
    cleaned = re.sub(r"^[，,。.、\s]+|[，,。.、\s]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or "（没说做什么）"


class ReminderStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.items = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            return [x for x in data if isinstance(x, dict) and x.get("at")]
        except Exception:  # noqa: BLE001
            return []

    def _save(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, conv: str, at: float, text: str) -> dict:
        with self.lock:
            item = {"id": uuid.uuid4().hex[:6], "conv": conv, "at": float(at),
                    "text": text, "done": False}
            self.items.append(item)
            self._save()
            return item

    def list(self, conv: str | None = None):
        with self.lock:
            return [x for x in self.items if not x.get("done")
                    and (conv is None or x.get("conv") == conv)]

    def remove(self, rid: str) -> bool:
        with self.lock:
            for x in self.items:
                if x.get("id") == rid and not x.get("done"):
                    x["done"] = True
                    self._save()
                    return True
            return False

    def due(self, now: float):
        with self.lock:
            out = [x for x in self.items if not x.get("done") and float(x.get("at") or 0) <= now]
            for x in out:
                x["done"] = True
            if out:
                self._save()
            return out


def fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class ReminderService:
    """每 15 秒扫一次，到点就让桥接发消息。"""

    def __init__(self, bridge, path: str, interval: float = 15.0):
        self.bridge = bridge
        self.store = ReminderStore(path)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reminders")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                for item in self.store.due(time.time()):
                    self._fire(item)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def _fire(self, item: dict):
        msg = f"⏰ 提醒：{item.get('text')}"
        try:
            self.bridge.send_to_conv(item.get("conv") or "", msg)
        except Exception:  # noqa: BLE001
            pass

    # -- 命令 ---------------------------------------------------------------
    def handle_command(self, text: str, conv: str):
        """返回 (是否处理了, 回复文本)。"""
        raw = text.strip()
        low = raw.lower()
        if low in ("/提醒", "/reminders", "/待办"):
            items = self.store.list(conv)
            if not items:
                return True, "没有待办。发「提醒我明天八点开会」。"
            lines = [f"{i+1}. {fmt_ts(x['at'])} · {x['text']}（{x['id']}）"
                     for i, x in enumerate(sorted(items, key=lambda y: y["at"]))]
            return True, "待办：\n" + "\n".join(lines)

        if low.startswith("/删提醒") or low.startswith("/取消提醒"):
            parts = raw.split()
            if len(parts) < 2:
                return True, "用法：/删提醒 编号或 id"
            key = parts[1]
            items = sorted(self.store.list(conv), key=lambda y: y["at"])
            target = None
            if key.isdigit() and 1 <= int(key) <= len(items):
                target = items[int(key) - 1]
            else:
                target = next((x for x in items if x["id"] == key), None)
            if not target:
                return True, "没找到这条提醒。"
            self.store.remove(target["id"])
            return True, "删掉了：" + target["text"]

        if "提醒" in raw or "叫我" in raw or "记得" in raw:
            at = parse_when(raw)
            if at is None:
                return False, ""      # 没时间，交给模型正常聊
            body = strip_time_words(raw)
            item = self.store.add(conv, at, body)
            return True, f"好，{fmt_ts(item['at'])} 提醒你：{body}"
        return False, ""
