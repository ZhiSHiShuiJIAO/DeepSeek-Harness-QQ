# -*- coding: utf-8 -*-
"""QQ 机器人整合包 —— 极简界面。

填 QQ 号 + API Key，选人设，点「启动」。
"""

import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import messagebox, ttk

KIT = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(KIT, "core")
PERSONAS = os.path.join(KIT, "personas")
CONFIG = os.path.join(CORE, "config.json")
PERSONA = os.path.join(CORE, "persona.txt")
LOG = os.path.join(CORE, "bridge.log")

TOOLTIP_TEXT = r"""风控具体做什么

· 发送抖动：每条消息发出前随机等 0–400 毫秒，
  让节奏不像程序定时

· 语音限频：每小时最多 8 条语音，超出改成打字

· 表情限频：每小时最多 10 张，超出就不发

· 静默时段：0:00–7:00 不发语音和表情（文字照常）

· 热身期：启动后前 N 分钟只发文字，默认 0

改哪里：core\config.json 的 risk 段
  例如 "voice_per_hour": 4 就是每小时最多 4 条

说句实话：这些只降低「像机器人」的特征，
挡不住第三方协议端本身的封号风险。"""

APP_NAME = "大肥鱼QQ"
AUTHOR = "揽月（LanYue）"
VERSION = "1.2.0"
UPDATE_URL = os.environ.get("DFF_UPDATE_URL") or ""

BRIDGE_HEALTH = "http://127.0.0.1:8787/health"
ONEBOT_STATUS = "http://127.0.0.1:3000/get_status"
NAPCAT_URL = "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.OneKey.zip"

# ---- 配色：浅色纸感，黑白灰 + 一点绿/红 ----
BG = "#F5F9FE"
CARD = "#FFFFFF"
LINE = "#E1EBF6"
TEXT = "#1E2A38"
MUTED = "#6C7C8E"
FAINT = "#A7B6C6"
DARK = "#2F6FE4"
OK = "#2E9E6B"
ERR = "#D9534F"

F = "Segoe UI"
F_TITLE = (F, 17)
F_SUB = (F, 9)
F_LABEL = (F, 9)
F_BODY = (F, 10)
F_MONO = ("Consolas", 9)


def http_json(url, payload=None, timeout=2.5):
    try:
        data = json.dumps(payload or {}).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _cli_for(exe_path):
    """cli.js 在 app.asar 里，普通 Python 看不到，只校验 asar 存在。"""
    base = os.path.dirname(exe_path)
    asar = os.path.join(base, "resources", "app.asar")
    if not os.path.exists(asar):
        return None
    return os.path.join(asar, "dsh", "node_modules", "@deepseek-ai",
                        "dsh-desktop-host", "lib", "cli.js")


def detect_dsh():
    exe = os.environ.get("DSH_EXE") or ""
    if exe and os.path.exists(exe):
        c = os.environ.get("DSH_CLI") or _cli_for(exe)
        if c and os.path.exists(c):
            return exe, c

    try:
        out = subprocess.run(["wmic", "process", "where",
                              "name='DeepSeek Harness.exe'", "get", "ExecutablePath"],
                             capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.lower().endswith(".exe") and os.path.exists(line):
                c = _cli_for(line)
                if c:
                    return line, c
    except Exception:  # noqa: BLE001
        pass

    roots = [r"C:\Program Files", r"C:\Program Files (x86)",
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
             os.environ.get("LOCALAPPDATA", ""), "D:\\", "C:\\"]
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir():
                continue
            cand = os.path.join(e.path, "DeepSeek Harness.exe")
            if os.path.exists(cand):
                c = _cli_for(cand)
                if c:
                    return cand, c
            try:
                subs = list(os.scandir(e.path))
            except OSError:
                continue
            for s in subs:
                if not s.is_dir():
                    continue
                deep = os.path.join(s.path, "DeepSeek Harness.exe")
                if os.path.exists(deep):
                    c = _cli_for(deep)
                    if c:
                        return deep, c
    return None, None


def find_napcat():
    """找 NapCat 目录（含 launcher.bat 的那层）。"""
    seen = []
    cand = [os.environ.get("NAPCAT_DIR") or "",
            os.path.join(KIT, "napcat"),
            os.path.join(os.path.dirname(KIT), "qq-bridge", "napcat"),
            r"D:\NapCat", r"C:\NapCat", r"D:\napcat", r"C:\napcat",
            os.path.join(os.path.expanduser("~"), "NapCat"),
            os.path.join(os.path.expanduser("~"), "Desktop", "NapCat")]
    for c in cand:
        if c and os.path.exists(os.path.join(c, "launcher.bat")) and c not in seen:
            return c
        if c:
            seen.append(c)
    return None


def napcat_state(napcat_dir):
    """问 NapCat 的 WebUI：登录到哪一步了。失败返回 None。"""
    if not napcat_dir:
        return None
    webui = os.path.join(napcat_dir, "config", "webui.json")
    try:
        cfg = json.load(open(webui, encoding="utf-8-sig"))
        token = cfg.get("token") or ""
        port = cfg.get("port") or 6099
    except Exception:  # noqa: BLE001
        return None
    if not token:
        return None
    base = "http://127.0.0.1:%s" % port
    try:
        h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
        req = urllib.request.Request(base + "/api/auth/login",
                                     data=json.dumps({"hash": h}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.2) as r:
            cred = json.loads(r.read().decode("utf-8", "replace"))["data"]["Credential"]
        req = urllib.request.Request(base + "/api/QQLogin/CheckLoginStatus",
                                     data=b"{}",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + cred})
        with urllib.request.urlopen(req, timeout=1.2) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("data") or {}
    except Exception:  # noqa: BLE001
        return None


def _ver_tuple(v):
    parts = []
    for p in str(v).replace("-", ".").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _blend3(c1, c2, t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)


def _rr(canvas, x1, y1, x2, y2, r, fill, outline=""):
    """画圆角矩形（用平滑多边形近似）。"""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, splinesteps=24,
                                 fill=fill, outline=outline, width=1)


class RButton(tk.Canvas):
    """圆角按钮。"""

    def __init__(self, parent, text, cmd, primary=False, width=None, height=34):
        bg = parent.cget("bg")
        w = width or max(76, len(text) * 15 + 36)
        super().__init__(parent, width=w, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.cmd, self.primary = text, cmd, primary
        self.w, self.h = w, height
        self.hover = False
        self.enabled = True
        self.bind("<Enter>", self._in)
        self.bind("<Leave>", self._out)
        self.bind("<Button-1>", self._click)
        self.redraw()

    def _in(self, _=None):
        self.hover = True
        self.redraw()

    def _out(self, _=None):
        self.hover = False
        self.redraw()

    def _click(self, _=None):
        if self.enabled and self.cmd:
            self.cmd()

    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self.redraw()

    def redraw(self):
        self.delete("all")
        if self.primary:
            fill = "#4A84F0" if (self.hover and self.enabled) else DARK
            if not self.enabled:
                fill = "#A9C4EA"
            _rr(self, 1, 1, self.w - 1, self.h - 1, 12, fill)
            color = "#FFFFFF"
        else:
            fill = "#EAF2FD" if (self.hover and self.enabled) else CARD
            _rr(self, 1, 1, self.w - 1, self.h - 1, 12, fill, LINE)
            color = TEXT if self.enabled else FAINT
        self.create_text(self.w / 2, self.h / 2, text=self.text,
                         font=(F, 10, "bold") if self.primary else (F, 10),
                         fill=color)


def _gradient(canvas, w, h, top, bottom):
    """竖渐变：用一行行细线铺，成本极低。"""
    canvas.delete("grad")
    steps = max(2, h)
    for y in range(steps):
        t = y / (steps - 1)
        col = "#%02x%02x%02x" % tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        canvas.create_line(0, y, w, y, fill=col, tags="grad")
    canvas.tag_lower("grad")


class Tooltip:
    """悬停 500ms 后在鼠标下方弹出说明。"""

    def __init__(self, widget, text, delay=500):
        self.widget, self.text, self.delay = widget, text, delay
        self._job = None
        self._win = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self):
        if self._win or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_geometry("+%d+%d" % (x, y))
        box = tk.Frame(win, bg="#FFFFFF", highlightthickness=1,
                       highlightbackground=LINE)
        box.pack()
        tk.Label(box, text=self.text, bg="#FFFFFF", fg=TEXT, font=(F, 9),
                 justify="left", anchor="w", padx=12, pady=10).pack()
        self._win = win

    def _hide(self, _=None):
        self._cancel()
        if self._win:
            self._win.destroy()
            self._win = None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc = None
        self.logq = queue.Queue()
        self.healthq = queue.Queue()
        self._log_size = -1
        self._ticks = 0
        self._dsh = (None, None)
        self._napcat = find_napcat()
        self._qr_mtime = -1
        self._qr_img = None
        self._login_state = ""
        self._setup_style()
        self._build()
        self.load_config()
        threading.Thread(target=self._poller, daemon=True).start()
        threading.Thread(target=self._detector, daemon=True).start()
        self.root.after(700, self.tick)

    # ---------------- 后台：网络和探测都不许卡界面 ----------------
    def _poller(self):
        while True:
            bridge = http_json(BRIDGE_HEALTH, timeout=0.6)
            onebot = http_json(ONEBOT_STATUS, {}, timeout=0.6)
            state = napcat_state(self._napcat)
            try:
                self.healthq.put_nowait((bridge, onebot, state))
            except queue.Full:
                pass
            time.sleep(2.0)

    def _detector(self):
        try:
            self._dsh = detect_dsh()
        except Exception:  # noqa: BLE001
            self._dsh = (None, None)


    TIPS = {
        "启动": "拉起桥接进程\n启动后状态灯会变绿，日志开始滚动。\n关机/睡眠会中断，需要重新点一次。",
        "停止": "结束桥接进程\nQQ 会继续在线（NapCat 不管），但不再回复消息。",
        "保存配置": "把界面上的设置写进 core\\config.json，并把人设复制成 core\\persona.txt\n点「启动」时会自动保存一次。",
        "检查更新": "连远端 version.json 比对版本\n只提示、不自动替换文件；没配地址时会告诉你该怎么写。",
        "打开目录": "打开 core 文件夹\n配置、日志、人设、沙箱都在里面。",
        "预览": "看这个人设的全文\n想知道她会怎么说话，先看这个。",
        "启动 NapCat": "以管理员身份启动 NapCat\n会弹 UAC，点「是」；QQ 登录窗随后出现。\n只在第一次或掉线后需要点。",
        "刷新二维码": "重新读取二维码图片\n如果 NapCat 没生成新码，读到的还是原来那张——\n那就用 QQ 窗口里显示的那个扫。",
        "查余额": "查 DeepSeek 账户余额\n用的是界面上填的 Key，后台查，不卡界面。",
        "语音": "允许她发语音\n需要联网合成（edge）；关掉后只打字。",
        "表情包": "允许她发表情包\n图片放在 core\\_sticker 里，/sticker 可以看库存。",
        "唱歌": "允许 /唱 触发歌声合成\n歌单在 core\\sandbox\\sing\\songs。",
        "余额": "允许在对话里问「还有多少余额」\n关掉后她会当作普通聊天处理。",
        "风控": TOOLTIP_TEXT,
        "联网": "允许她联网检索\n关掉后只用已有知识，查不到就说不知道，不会编。",
    }

    def _wire_tooltips(self):
        """按控件文字给按钮/勾选框挂说明，省得逐个写。"""

        def walk(widget, depth=0):
            if depth > 8:
                return
            for child in widget.winfo_children():
                try:
                    text = str(child.cget("text"))
                except Exception:  # noqa: BLE001
                    text = ""
                if text and text in self.TIPS:
                    try:
                        Tooltip(child, self.TIPS[text])
                    except Exception:  # noqa: BLE001
                        pass
                walk(child, depth + 1)

        walk(self.page)

    # ---------------- 外观 ----------------
    def _setup_style(self):
        self.root.configure(bg=BG)
        self.root.title(APP_NAME)
        self.root.geometry("740x780")
        self.root.minsize(700, 560)

        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TEntry", fieldbackground=CARD, background=CARD, foreground=TEXT,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     insertcolor=TEXT, relief="flat", padding=(8, 6))
        st.map("TEntry", bordercolor=[("focus", "#C9C9CE")])
        st.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                     arrowcolor=MUTED, relief="flat", padding=(6, 5))
        st.configure("Small.TButton", font=(F, 9), background=CARD, foreground=TEXT,
                     bordercolor=LINE, relief="flat", padding=(10, 4))
        st.map("Small.TButton", background=[("active", "#F0F0F2")])

    def _card(self, parent, gap=14):
        """圆角白卡片：用 Canvas 画圆角底，内容放在内嵌 Frame 里。"""
        cv = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0, height=20)
        cv.pack(fill="x", padx=22, pady=(0, gap))
        body = tk.Frame(cv, bg=CARD)
        win = cv.create_window(18, 18, window=body, anchor="nw")

        def paint():
            cv.delete("bg")
            w, h = cv.winfo_width(), cv.winfo_height()
            if w > 4 and h > 4:
                _rr(cv, 1, 1, w - 1, h - 1, 16, CARD, LINE)
                cv.tag_lower("bg")

        def on_canvas(e):
            cv.coords(win, 18, 18)
            cv.itemconfigure(win, width=max(1, e.width - 36))
            paint()

        def on_body(e):
            want = body.winfo_reqheight() + 36
            if abs(want - int(cv["height"])) > 1:
                cv.configure(height=want)
                paint()

        cv.bind("<Configure>", on_canvas)
        body.bind("<Configure>", on_body)
        return body

    def _row(self, parent, label, var, hint, secret=False):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=16, pady=5)
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=F_LABEL,
                 width=11, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, font=F_BODY,
                  show="•" if secret else "").pack(side="left", fill="x",
                                                   expand=True, padx=(10, 10))
        tk.Label(row, text=hint, bg=CARD, fg=FAINT, font=(F, 8)).pack(side="right")
        ent = row.winfo_children()[1]
        Tooltip(ent, "%s\n%s" % (label, hint))

    def _button(self, parent, text, cmd, primary=False):
        return RButton(parent, text, cmd, primary=primary)

    @staticmethod
    def _dot(canvas, color):
        canvas.delete("all")
        canvas.create_oval(1, 1, 7, 7, fill=color, outline=color)

    def _build(self):
        # ---- 可滚动页面 ----
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=bar.set)

        self.page = tk.Frame(self.canvas, bg=BG)
        self._page_id = self.canvas.create_window((0, 0), window=self.page, anchor="nw")

        self.page.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._page_id, width=e.width))
        self.root.bind_all("<MouseWheel>",
                           lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        head = tk.Canvas(self.page, height=172, bg=BG, highlightthickness=0, bd=0)
        head.pack(fill="x")

        HEAD_H = 172
        blobs = [
            # (x 比例, y 比例, 半径比例, 颜色, 相位)
            (0.18, 0.30, 0.55, (255, 255, 255), 0.0),
            (0.52, 0.72, 0.62, (198, 224, 249), 1.7),
            (0.86, 0.24, 0.50, (214, 232, 252), 3.1),
            (0.70, 0.95, 0.45, (232, 240, 250), 4.4),
        ]

        def paint_head(t=0.0):
            w = max(320, head.winfo_width())
            head.delete("all")
            # 底色：竖向浅蓝渐变
            steps = 16
            for i in range(steps):
                y1 = int(HEAD_H * i / steps)
                y2 = int(HEAD_H * (i + 1) / steps) + 1
                k = i / max(1, steps - 1)
                col = _blend3((214, 231, 249), (247, 250, 254), k)
                head.create_line(0, y1, w, y1, fill=_hex(col), tags="bg")
                head.create_rectangle(0, y1, w, y2, outline="", fill=_hex(col), tags="bg")
            # 液态光斑：同心圆由外到内逐渐变亮，位置缓慢漂移
            for (bx, by, br, color, phase) in blobs:
                cx = w * (bx + 0.035 * math.sin(t * 0.55 + phase))
                cy = HEAD_H * (by + 0.10 * math.cos(t * 0.42 + phase * 1.3))
                r0 = min(w, HEAD_H) * br
                rings = 10
                for i in range(rings):
                    rr = r0 * (1 - i / rings)
                    mix = i / rings
                    col = _blend3(_blend3((214, 231, 249), color, 0.85), color, mix)
                    head.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                                     outline="", fill=_hex(col), tags="blob")
            # 文字压在最上层
            head.create_text(30, 58, text=APP_NAME, anchor="w",
                             font=(F, 24, "bold"), fill="#1B3A5C", tags="txt")
            head.create_text(31, 108,
                             text="生活奇奇怪怪，肥鱼与你同在\n愿你也有一个独属于自己的肥鱼",
                             anchor="w", justify="left",
                             font=(F, 9), fill="#6F8AA6", tags="txt")

        def animate(step=0):
            try:
                paint_head(step * 0.16)
            except tk.TclError:
                return
            try:
                self.root.after(120, lambda: animate(step + 1))
            except tk.TclError:
                pass

        head.bind("<Configure>", lambda e: paint_head(self._head_t))
        self._head_t = 0.0
        animate()

        # ---- 风险提示条 ----
        warn = tk.Canvas(self.page, height=46, bg=BG, highlightthickness=0, bd=0)
        warn.pack(fill="x", padx=22, pady=(6, 14))

        def draw_warn(event=None):
            w = max(320, warn.winfo_width())
            warn.delete("all")
            _rr(warn, 1, 1, w - 1, 45, 12, "#FEF5E7", "#F2DFB9")
            warn.create_text(18, 23, anchor="w", font=(F, 9), fill="#96671A",
                             text="!  使用第三方协议端（NapCat）登录 QQ 违反官方用户协议，存在封号风险，请务必使用小号，谨慎使用。")

        warn.bind("<Configure>", draw_warn)

        self.var_owner = tk.StringVar()
        self.var_key = tk.StringVar()
        self.var_bot = tk.StringVar()

        # ---- 扫码登录卡 ----
        c0 = self._card(self.page)
        tk.Label(c0, text="·  QQ 登录", bg=CARD, fg=FAINT, font=(F, 8),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 6))

        body0 = tk.Frame(c0, bg=CARD)
        body0.pack(fill="x", padx=16, pady=(0, 14))

        self.qr = tk.Label(body0, bg="#F7FBFF", width=22, height=11,
                           text="二维码会显示在这里", fg=FAINT, font=(F, 8),
                           highlightthickness=1, highlightbackground=LINE)
        self.qr.pack(side="left", anchor="n")

        right = tk.Frame(body0, bg=CARD)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        self.lbl_login = tk.Label(right, text="NapCat 未启动", bg=CARD, fg=MUTED,
                                  font=F_LABEL, anchor="w", justify="left",
                                  wraplength=330)
        self.lbl_login.pack(fill="x", anchor="w")

        self.lbl_napcat_path = tk.Label(right, text="", bg=CARD, fg=FAINT,
                                        font=(F, 8), anchor="w", wraplength=330,
                                        justify="left")
        self.lbl_napcat_path.pack(fill="x", anchor="w", pady=(6, 10))

        rbtn = tk.Frame(right, bg=CARD)
        rbtn.pack(fill="x", anchor="w")
        self._button(rbtn, "启动 NapCat", self.launch_napcat).pack(side="left")
        self._button(rbtn, "刷新二维码", self.reload_qr).pack(side="left", padx=(8, 0))

        link = tk.Label(right, text="一键下载 NapCat（官方 Windows 安装包）↓", bg=CARD, fg="#2F6FE4",
                        font=(F, 9, "underline"), cursor="hand2", anchor="w")
        link.pack(fill="x", anchor="w", pady=(12, 0))
        link.bind("<Button-1>", lambda e: self.open_url(NAPCAT_URL))

        # ---- 连接 ----
        c1 = self._card(self.page)
        tk.Label(c1, text="·  连接", bg=CARD, fg=FAINT, font=(F, 8),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        self._row(c1, "你的 QQ 号", self.var_owner, "只有这个号能指挥它")
        self._row(c1, "DeepSeek Key", self.var_key, "platform.deepseek.com 申请", secret=True)
        self._row(c1, "机器人 QQ 号", self.var_bot, "登录后自动填")
        tk.Frame(c1, bg=CARD, height=10).pack()

        # ---- 人设 ----
        c2 = self._card(self.page)
        tk.Label(c2, text="·  人设", bg=CARD, fg=FAINT, font=(F, 8),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 8))
        row = tk.Frame(c2, bg=CARD)
        row.pack(fill="x", padx=16, pady=(0, 4))
        self.var_persona = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.var_persona, state="readonly",
                                  font=F_BODY, width=26)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.update_summary())
        ttk.Button(row, text="预览", style="Small.TButton",
                   command=self.preview).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="打开目录", style="Small.TButton",
                   command=lambda: self.open_dir(PERSONAS)).pack(side="left", padx=(6, 0))
        self.lbl_summary = tk.Label(c2, text="", bg=CARD, fg=MUTED, font=(F, 9),
                                    anchor="w", justify="left", wraplength=600)
        self.lbl_summary.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(c2, text="往 personas 里放 .txt 就能自己加", bg=CARD, fg=FAINT,
                 font=F_SUB, anchor="w").pack(fill="x", padx=16, pady=(0, 14))

        # ---- 功能 ----
        c3 = self._card(self.page)
        tk.Label(c3, text="·  功能", bg=CARD, fg=FAINT, font=(F, 8),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 8))
        vrow = tk.Frame(c3, bg=CARD)
        vrow.pack(fill="x", padx=16, pady=(0, 6))
        self.var_voice = tk.BooleanVar(value=True)
        self.var_sticker = tk.BooleanVar(value=True)
        self.var_sing = tk.BooleanVar(value=True)
        self.var_balance = tk.BooleanVar(value=True)
        self.var_risk = tk.BooleanVar(value=True)
        self.var_net = tk.BooleanVar(value=True)
        self.var_engine = tk.StringVar(value="edge")
        tk.Checkbutton(vrow, text="语音", variable=self.var_voice, bg=CARD,
                       fg=TEXT, font=F_BODY, activebackground=CARD, selectcolor=CARD,
                       bd=0, highlightthickness=0).pack(side="left")
        ttk.Combobox(vrow, textvariable=self.var_engine, state="readonly",
                     font=(F, 9), width=11,
                     values=["edge", "gpt-sovits", "mimo"]).pack(side="left", padx=(8, 22))
        for text, var in (("表情包", self.var_sticker),
                          ("唱歌", self.var_sing),
                          ("余额", self.var_balance),
                          ("风控", self.var_risk),
                          ("联网", self.var_net)):
            cb = tk.Checkbutton(vrow, text=text, variable=var, bg=CARD, fg=TEXT,
                                font=F_BODY, activebackground=CARD, selectcolor=CARD,
                                bd=0, highlightthickness=0, cursor="hand2")
            cb.pack(side="left", padx=(0, 18))

        tk.Label(c3, text="关掉的项不会触发；余额也可以直接在对话里问「还有多少余额」",
                 bg=CARD, fg=FAINT, font=F_SUB, anchor="w").pack(fill="x", padx=16, pady=(0, 14))

        # ---- 操作 ----
        bar2 = tk.Frame(self.page, bg=BG)
        bar2.pack(fill="x", padx=22, pady=(2, 12))
        self.btn_start = self._button(bar2, "启动", self.start, primary=True)
        self.btn_start.pack(side="left")
        self.btn_stop = self._button(bar2, "停止", self.stop)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_stop.set_enabled(False)
        self._button(bar2, "保存配置", self.save).pack(side="left", padx=(8, 0))
        self._button(bar2, "打开目录", lambda: self.open_dir(CORE)).pack(side="right")
        self._button(bar2, "检查更新", self.check_update).pack(side="right", padx=(0, 8))

        # ---- 状态 + 日志（浅色，不再有黑块）----
        c4 = self._card(self.page, gap=10)
        srow = tk.Frame(c4, bg=CARD)
        srow.pack(fill="x", padx=16, pady=(12, 8))
        self.dot_napcat = tk.Canvas(srow, width=8, height=8, bg=CARD, highlightthickness=0)
        self.dot_napcat.pack(side="left", pady=(2, 0))
        self.lbl_napcat = tk.Label(srow, text="NapCat 未连接", bg=CARD, fg=MUTED,
                                   font=F_LABEL)
        self.lbl_napcat.pack(side="left", padx=(7, 18))
        self.dot_bridge = tk.Canvas(srow, width=8, height=8, bg=CARD, highlightthickness=0)
        self.dot_bridge.pack(side="left", pady=(2, 0))
        self.lbl_bridge = tk.Label(srow, text="机器人未启动", bg=CARD, fg=MUTED,
                                   font=F_LABEL)
        self.lbl_bridge.pack(side="left", padx=(7, 0))
        self._dot(self.dot_napcat, FAINT)
        self._dot(self.dot_bridge, FAINT)

        brow = tk.Frame(c4, bg=CARD)
        brow.pack(fill="x", padx=16, pady=(0, 10))
        self.lbl_balance = tk.Label(brow, text="余额 —", bg=CARD, fg=MUTED, font=F_LABEL)
        self.lbl_balance.pack(side="left")
        RButton(brow, "查余额", self.refresh_balance, width=72, height=28
                ).pack(side="left", padx=(10, 0))
        tk.Label(brow, text="当前人设文件：core\\persona.txt", bg=CARD, fg=FAINT,
                 font=(F, 8)).pack(side="right")

        tk.Frame(c4, bg=LINE, height=1).pack(fill="x", padx=16)
        self.txt = tk.Text(c4, height=6, bg="#F7FAFD", fg="#5A6B7C", font=F_MONO,
                           bd=0, highlightthickness=0, padx=12, pady=8, wrap="none")
        self.txt.pack(fill="both", expand=True, padx=1, pady=(1, 1))
        self.txt.configure(state="disabled")

        tk.Label(self.page, text="第一次用：点「启动 NapCat」→ 扫码 → 配好 HTTP 上报 → 点「启动」",
                 bg=BG, fg=FAINT, font=F_SUB, anchor="w").pack(fill="x", padx=24, pady=(4, 4))
        self._wire_tooltips()

        tk.Label(self.page, text="by %s · v%s · 仅供个人使用，禁止商用和改名分发" % (AUTHOR, VERSION),
                 bg=BG, fg=FAINT, font=(F, 8), anchor="w").pack(fill="x", padx=24, pady=(0, 16))

    # ---------------- 人设摘要 / 余额 ----------------
    def update_summary(self):
        name = self.var_persona.get()
        path = os.path.join(PERSONAS, name + ".txt") if name else ""
        text = ""
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            lines = []
        for i, ln in enumerate(lines):
            if ln.strip().startswith("# 你是谁"):
                for nxt in lines[i + 1:]:
                    if nxt.strip():
                        text = nxt.strip()
                        break
                break
        self.lbl_summary.configure(text=text)

    def refresh_balance(self):
        key = self.var_key.get().strip()
        if not key:
            self.lbl_balance.configure(text="余额 —（先填 Key）", fg=MUTED)
            return
        self.lbl_balance.configure(text="余额 查询中…", fg=MUTED)

        def work():
            try:
                req = urllib.request.Request(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                infos = data.get("balance_infos") or []
                txt = " / ".join(
                    ("¥" if (i.get("currency") or "CNY") == "CNY" else "$")
                    + str(i.get("total_balance", "0")) for i in infos) or "—"
                color = OK if data.get("is_available", True) else ERR
            except Exception:  # noqa: BLE001
                txt, color = "查不到（Key 或网络）", ERR
            self.root.after(0, lambda: self.lbl_balance.configure(
                text="余额 " + txt, fg=color))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 扫码登录 ----------------
    def launch_napcat(self):
        if not self._napcat:
            messagebox.showinfo(
                "先装 NapCat",
                "没找到 NapCat。\n\n去 GitHub 搜 NapNeko/NapCatQQ，下载 NapCat.Shell.zip，\n"
                "解压到 D:\\NapCat 或整合包旁边的 napcat 文件夹，再点这个按钮。")
            return
        target = os.path.join(self._napcat, "launcher.bat")
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "Start-Process -FilePath '%s' -Verb RunAs" % target],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            return
        self.lbl_login.configure(text="正在启动 NapCat（会弹 UAC，点「是」）…", fg=TEXT)

    def reload_qr(self):
        self._qr_mtime = -1
        self._show_qr(force=True)
        self.lbl_login.configure(text="已重新读取二维码；扫码后用手机点「确认登录」", fg=TEXT)

    def _show_qr(self, force=False):
        if not self._napcat:
            return
        path = os.path.join(self._napcat, "cache", "qrcode.png")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if not force and mtime == self._qr_mtime:
            return
        self._qr_mtime = mtime
        try:
            img = tk.PhotoImage(file=path)
            self._qr_img = img
            self.qr.configure(image=img, text="", width=img.width(), height=img.height())
        except Exception:  # noqa: BLE001
            self.qr.configure(text="二维码读不出来\n用 QQ 窗口里的那个扫", image="")

    def _apply_login(self, state):
        if not state:
            if self._login_state != "none":
                self._login_state = "none"
                self.lbl_login.configure(text="NapCat 未启动", fg=MUTED)
                self.lbl_napcat_path.configure(
                    text=("目录：" + self._napcat) if self._napcat else "没找到 NapCat，点「启动 NapCat」看说明")
            return

        self._show_qr()
        phase = str(state.get("loginPhase") or "")
        if state.get("isLogin"):
            uin = str(state.get("uin") or "")
            self._login_state = "in"
            self.lbl_login.configure(text="已登录" + (" · " + uin if uin else ""), fg=OK)
            if uin and uin.isdigit() and uin != self.var_bot.get():
                self.var_bot.set(uin)
            return

        text = {
            "waiting_qrcode": "等待扫码：用手机 QQ 扫左边这个码",
            "qrcode_scanned": "已扫码，快在手机上点「确认登录」",
            "qrcode_expired": "二维码过期了，点「刷新二维码」",
        }.get(phase, "等待扫码…")
        self._login_state = phase
        self.lbl_login.configure(text=text, fg=MUTED if phase == "waiting_qrcode" else TEXT)

    def open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开", str(exc))

    def check_update(self):
        url = UPDATE_URL
        if not url:
            try:
                url = str(((json.load(open(CONFIG, encoding="utf-8-sig"))
                            .get("update") or {}).get("url")) or "")
            except Exception:  # noqa: BLE001
                url = ""
        if not url:
            messagebox.showinfo(
                "检查更新",
                "还没配更新地址。\n\n在 core\\config.json 里加一段：\n"
                '  "update": { "url": "https://你的地址/version.json" }\n\n'
                'version.json 内容：{"version": "1.2.0", "url": "下载页", "notes": "改了什么"}')
            return

        def work():
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                new = str(data.get("version") or "")
                if _ver_tuple(new) > _ver_tuple(VERSION):
                    msg = "发现新版本 %s（当前 %s）\n\n%s" % (new, VERSION, data.get("notes") or "")
                    self.root.after(0, lambda: self._update_prompt(msg, data.get("url")))
                else:
                    self.root.after(0, lambda: messagebox.showinfo(
                        "检查更新", "已是最新版（%s）" % VERSION))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: messagebox.showerror(
                    "检查更新", "连不上更新服务器：%s" % exc))

        threading.Thread(target=work, daemon=True).start()

    def _update_prompt(self, msg, page):
        if page and messagebox.askyesno("有新版本", msg + "\n\n要打开下载页吗？"):
            self.open_url(page)
        else:
            messagebox.showinfo("有新版本", msg)

    def open_dir(self, path):
        try:
            os.startfile(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开", str(exc))

    def preview(self):
        name = self.var_persona.get()
        if not name:
            return
        path = os.path.join(PERSONAS, name + ".txt")
        try:
            body = open(path, encoding="utf-8").read()
        except OSError as exc:
            messagebox.showerror("读不到人设", str(exc))
            return
        win = tk.Toplevel(self.root)
        win.title("人设预览 · " + name)
        win.geometry("620x520")
        win.configure(bg=BG)
        t = tk.Text(win, wrap="word", bg=CARD, fg=TEXT, font=(F, 10), bd=0,
                    highlightthickness=1, highlightbackground=LINE, padx=16, pady=14)
        t.pack(fill="both", expand=True, padx=16, pady=16)
        t.insert("1.0", body)
        t.configure(state="disabled")

    # ---------------- 配置 ----------------
    def load_config(self):
        names = sorted(n[:-4] for n in os.listdir(PERSONAS) if n.endswith(".txt"))
        self.combo["values"] = names
        if names:
            self.combo.set(names[0])
            self.update_summary()
        if not os.path.exists(CONFIG):
            return
        try:
            cfg = json.load(open(CONFIG, encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return
        users = cfg.get("allowed_users") or []
        if users:
            self.var_owner.set(str(users[0]))
        self.var_key.set((cfg.get("engine") or {}).get("api_key") or "")
        tts = cfg.get("tts") or {}
        self.var_voice.set(bool(tts.get("enabled", True)))
        self.var_sticker.set(bool((cfg.get("sticker") or {}).get("enabled", True)))
        self.var_sing.set(bool((cfg.get("sing") or {}).get("enabled", True)))
        self.var_balance.set(bool((cfg.get("balance") or {}).get("enabled", True)))
        self.var_risk.set(bool((cfg.get("risk") or {}).get("enabled", True)))
        self.var_net.set(bool((cfg.get("net") or {}).get("enabled", True)))
        prov = str(tts.get("provider") or "edge")
        self.var_engine.set(prov if prov in ("edge", "gpt-sovits", "mimo") else "edge")

    def build_config(self):
        base = {}
        tpl = os.path.join(KIT, "config.example.json")
        if os.path.exists(tpl):
            try:
                base = json.load(open(tpl, encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                base = {}
        owner = self.var_owner.get().strip()
        base["allowed_users"] = [int(owner)] if owner.isdigit() else []
        tts = base.setdefault("tts", {})
        tts["enabled"] = bool(self.var_voice.get())
        tts["provider"] = self.var_engine.get().strip() or "edge"
        base.setdefault("sticker", {})["enabled"] = bool(self.var_sticker.get())
        base.setdefault("sing", {})["enabled"] = bool(self.var_sing.get())
        base.setdefault("balance", {})["enabled"] = bool(self.var_balance.get())
        base.setdefault("risk", {})["enabled"] = bool(self.var_risk.get())
        base.setdefault("net", {})["enabled"] = bool(self.var_net.get())
        eng = base.setdefault("engine", {})
        eng["api_key"] = self.var_key.get().strip()
        eng["persona_file"] = "persona.txt"
        eng["cwd"] = os.path.join(CORE, "sandbox")
        exe, cli = self._dsh
        if exe and cli:
            eng["dsh_exe"], eng["cli_js"] = exe, cli
        return base

    def save(self, quiet=False):
        if not self.var_owner.get().strip().isdigit():
            messagebox.showwarning("还差一步", "「你的 QQ 号」要填数字。")
            return False
        if not self.var_key.get().strip():
            messagebox.showwarning("还差一步", "DeepSeek Key 没填。")
            return False
        cfg = self.build_config()
        os.makedirs((cfg.get("engine") or {}).get("cwd") or os.path.join(CORE, "sandbox"),
                    exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        name = self.var_persona.get()
        if name:
            src = os.path.join(PERSONAS, name + ".txt")
            if os.path.exists(src):
                shutil.copyfile(src, PERSONA)
        self.log("[配置] 已保存 · 人设 " + name)
        if not quiet:
            messagebox.showinfo("好了", "配置和人设都写好了。")
        return True

    # ---------------- 运行 ----------------
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if not self.save(quiet=True):
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = subprocess.Popen([sys.executable, os.path.join(CORE, "bridge.py")],
                                         cwd=CORE, env=env,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            return
        self.log("[启动] pid %s" % self.proc.pid)
        self.btn_start.set_enabled(False)
        self.btn_stop.set_enabled(True)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log("[停止] 已发送信号")
        self.btn_start.set_enabled(True)
        self.btn_stop.set_enabled(False)

    def log(self, text):
        self.logq.put(text)

    def _drain(self):
        try:
            while True:
                line = self.logq.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", time.strftime("%H:%M ") + line + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass

    def _tail_log(self):
        try:
            size = os.path.getsize(LOG)
        except OSError:
            return
        if size == self._log_size:
            return
        self._log_size = size
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                f.seek(max(0, size - 3000))
                for line in f.read().strip().splitlines()[-8:]:
                    self.log(line[:150])
        except OSError:
            pass

    def tick(self):
        self._ticks += 1
        self._drain()

        try:
            bridge, onebot, state = self.healthq.get_nowait()
        except queue.Empty:
            bridge, onebot, state = None, None, None
        else:
            self._apply_login(state)
            alive = bool(onebot and onebot.get("status") == "ok")
            self._dot(self.dot_napcat, OK if alive else ERR)
            self.lbl_napcat.configure(text="NapCat 已连接" if alive else "NapCat 未连接",
                                      fg=TEXT if alive else MUTED)
            if bridge:
                self._dot(self.dot_bridge, OK)
                self.lbl_bridge.configure(
                    text="运行中 · %s 个会话 · 已回 %s 条" % (bridge.get("sessions"),
                                                            bridge.get("replies")),
                    fg=TEXT)
            else:
                self._dot(self.dot_bridge, ERR)
                self.lbl_bridge.configure(text="机器人未启动", fg=MUTED)

        if self._ticks % 4 == 0:
            self._tail_log()

        if self.proc and self.proc.poll() is not None:
            self.log("[退出] 进程结束（code=%s），看 core\\bridge.log" % self.proc.returncode)
            self.proc = None
            self.btn_start.set_enabled(True)
            self.btn_stop.set_enabled(False)
        self.root.after(700, self.tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
