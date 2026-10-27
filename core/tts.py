#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音合成：把一段文字变成 QQ 能发的音频文件。

主用 edge-tts（微软在线神经语音，声音自然；依赖装在 `_vendor/`，不污染系统 Python）。
没装 edge-tts 时，`synthesize()` 会抛 TTSError，桥接会把这段文字降级成普通文本发出去，
不会把消息吞掉。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "_vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.append(VENDOR)   # 追加而不是前置：系统里装了新版时优先用系统的

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class TTSError(RuntimeError):
    pass


def _as_pct(value, default: str) -> str:
    """把 1.05 / "5" / "+5%" 统一成 edge-tts 要的 "+5%" 形式。"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        pct = int(round(value * 100)) - 100 if abs(value) <= 2 else int(round(value))
        return f"{pct:+d}%"
    text = str(value).strip()
    if text.endswith("%"):
        return text if text[0] in "+-" else "+" + text
    try:
        return f"{int(float(text)):+d}%"
    except ValueError:
        return default


def _as_hz(value, default: str) -> str:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return f"{int(round(value)):+d}Hz"
    text = str(value).strip()
    if text.lower().endswith("hz"):
        return text if text[0] in "+-" else "+" + text
    try:
        return f"{int(float(text)):+d}Hz"
    except ValueError:
        return default


def synthesize(text: str, out_path: str, tts_cfg: dict | None = None) -> str:
    """按 provider 合成；失败就按 mimo → gpt-sovits → edge 的顺序退，不吞消息。"""
    cfg = tts_cfg or {}
    text = (text or "").strip()
    if not text:
        raise TTSError("空文本")

    provider = str(cfg.get("provider") or "edge").lower()
    engines = {
        "mimo": lambda: _synth_mimo(text, out_path, cfg),
        "mimo-tts": lambda: _synth_mimo(text, out_path, cfg),
        "xiaomi": lambda: _synth_mimo(text, out_path, cfg),
        "gpt-sovits": lambda: gsv_synthesize(text, out_path, cfg),
        "gpt_sovits": lambda: gsv_synthesize(text, out_path, cfg),
        "gsv": lambda: gsv_synthesize(text, out_path, cfg),
        "edge": lambda: edge_synthesize(text, out_path, cfg),
    }
    order = [provider] + [k for k in ("mimo", "gpt-sovits", "edge") if k != provider]

    last = None
    for name in order:
        fn = engines.get(name)
        if fn is None:
            continue
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[tts] {name} failed ({exc!r}), trying next", flush=True)

    raise TTSError(f"所有语音引擎都失败了：{last}")


def edge_synthesize(text: str, out_path: str, tts_cfg: dict | None = None) -> str:
    """阻塞式合成，调用方在 worker 线程里跑，所以 asyncio.run 安全。"""
    cfg = tts_cfg or {}
    text = (text or "").strip()
    if not text:
        raise TTSError("空文本")
    try:
        import edge_tts  # noqa: PLC0415 - 延迟导入，缺依赖时只影响语音
    except Exception as exc:  # noqa: BLE001
        raise TTSError("edge-tts 未安装（qq-bridge/_vendor 不存在）") from exc

    voice = cfg.get("voice") or DEFAULT_VOICE
    rate = _as_pct(cfg.get("rate"), "+0%")
    pitch = _as_hz(cfg.get("pitch"), "+0Hz")
    volume = _as_pct(cfg.get("volume"), "+0%")
    retries = max(0, int(cfg.get("retries", 1)))

    async def _run() -> None:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
        await comm.save(out_path)

    last_exc = None
    for attempt in range(retries + 1):
        try:
            asyncio.run(_run())
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            last_exc = TTSError("合成结果为空")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise TTSError(f"语音合成失败：{last_exc}")


# --------------------------------------------------------------------------- #
# GPT-SoVITS（本地常驻服务，模型只加载一次）
# --------------------------------------------------------------------------- #

GSV_DEFAULT_URL = "http://127.0.0.1:9880"
_gsv_lock = threading.Lock()


def _gsv_root() -> str:
    return os.path.join(os.path.dirname(HERE), "gpt-sovits")


def _gsv_health(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _gsv_spawn(cfg: dict) -> None:
    script = cfg.get("gsv_server") or os.path.join(_gsv_root(), "gsv_server.py")
    if not os.path.exists(script):
        raise TTSError(f"找不到 gsv_server.py：{script}")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script), env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    print(f"[tts] started gpt-sovits server: {script}", flush=True)


def _ensure_gsv(cfg: dict) -> str:
    url = str(cfg.get("gsv_url") or GSV_DEFAULT_URL).rstrip("/")
    wait = float(cfg.get("gsv_boot_sec") or 180)
    h = _gsv_health(url)
    if h and h.get("ok"):
        return url
    with _gsv_lock:
        h = _gsv_health(url)
        if h and h.get("ok"):
            return url
        if h is None:                     # nothing listening yet
            _gsv_spawn(cfg)
        deadline = time.time() + wait
        while time.time() < deadline:
            h = _gsv_health(url)
            if h and h.get("ok"):
                print("[tts] gpt-sovits ready", flush=True)
                return url
            if h and h.get("error"):
                raise TTSError(f"gpt-sovits 启动失败：{h['error']}")
            time.sleep(2)
    raise TTSError("gpt-sovits 服务超时未就绪")


def gsv_synthesize(text: str, out_path: str, tts_cfg: dict | None = None) -> str:
    cfg = tts_cfg or {}
    url = _ensure_gsv(cfg)
    payload = {"text": text}
    if cfg.get("ref_audio"):
        payload["ref_audio"] = cfg["ref_audio"]
    if cfg.get("prompt_text"):
        payload["prompt_text"] = cfg["prompt_text"]
    if cfg.get("tune"):
        payload["tune"] = cfg["tune"]
    req = urllib.request.Request(url + "/tts", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(cfg.get("gsv_timeout_sec") or 240)) as r:
        wav = r.read()
    if len(wav) < 2000:
        raise TTSError("gpt-sovits 返回的音频为空")
    with open(out_path, "wb") as f:
        f.write(wav)
    return out_path


def cleanup_cache(directory: str, keep: int) -> None:
    """只保留最新的 keep 个音频，避免 _voice 越堆越大。"""
    try:
        files = [os.path.join(directory, n) for n in os.listdir(directory)
                 if n.lower().endswith((".mp3", ".wav", ".silk"))]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[max(1, keep):]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 小米 MiMo-TTS（OpenAI 兼容的 chat/completions）
# --------------------------------------------------------------------------- #

MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_DEFAULT_MODEL = "mimo-v2.5-tts"


def _synth_mimo(text: str, out_path: str, cfg: dict) -> str:
    key = str(cfg.get("mimo_key") or "").strip()
    if not key:
        raise TTSError("没配 tts.mimo_key（小米 MiMo 的 API Key）")
    model = cfg.get("mimo_model") or MIMO_DEFAULT_MODEL
    fmt = cfg.get("mimo_format") or "mp3"

    messages = []
    style = str(cfg.get("mimo_style") or "").strip()
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": text})

    audio = {"format": fmt}
    voice = str(cfg.get("mimo_voice") or "").strip()
    if "voiceclone" in model:
        ref = str(cfg.get("ref_audio") or "")
        if not ref or not os.path.isfile(ref):
            raise TTSError(f"克隆音色需要参考音频，找不到：{ref}")
        with open(ref, "rb") as f:
            raw = f.read()
        # MiMo 要的是 DataURL，不是裸 base64
        mime = "audio/mpeg" if ref.lower().endswith(".mp3") else "audio/wav"
        audio["voice"] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    elif voice:
        audio["voice"] = voice

    payload = {"model": model, "messages": messages, "audio": audio, "stream": False}
    req = urllib.request.Request(
        MIMO_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "api-key": key},
        method="POST",
    )
    raw = None
    last_err = None
    for attempt in range(4):          # 限流很紧，退避重试
        try:
            with urllib.request.urlopen(req, timeout=float(cfg.get("mimo_timeout_sec") or 120)) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last_err = f"mimo 返回 {exc.code}：{detail}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 3.0 * (2 ** attempt)
                print(f"[tts] mimo {exc.code}，{wait:.0f}s 后重试", flush=True)
                time.sleep(wait)
                continue
            raise TTSError(last_err) from exc
        except Exception as exc:  # noqa: BLE001
            last_err = f"连不上 mimo：{exc}"
            if attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise TTSError(last_err) from exc

    if raw is None:
        raise TTSError(last_err or "mimo 没有返回")

    try:
        data = json.loads(raw)
        b64 = data["choices"][0]["message"]["audio"]["data"]
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"mimo 返回里没有音频：{raw[:200]}") from exc
    blob = base64.b64decode(b64)
    if not blob:
        raise TTSError("mimo 返回了空音频")
    with open(out_path, "wb") as f:
        f.write(blob)
    return out_path
