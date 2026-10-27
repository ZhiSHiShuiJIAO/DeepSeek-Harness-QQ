"""把角色卡（SillyTavern / TavernAI / V2 / V3）转成 qq-bridge 的 persona.txt。

支持：
  - .png   （tEXt 块 "chara" 或 iTXt 块 "ccv3"，base64 JSON）
  - .json  （V1 平铺 / V2 带 data / V3 带 spec_version）

用法：
  python tools\\card2persona.py 蕾姆.png
  python tools\\card2persona.py 蕾姆.json --out ..\\..\\qq-bridge\\persona.txt
"""

import argparse
import base64
import json
import os
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "qq-bridge", "persona.txt"))

# 我们的固定尾部：卡片里没有、但桥接必须有的规则
TAIL = """

# 认真模式
碰到安全、健康、钱、法律，或者他明显撑不住了：立刻收住撒娇，认真、简短、有条理。
先说结论，再给步骤。

# 底线
- 不编事实、不编链接，不确定就说不确定。
- 不主动打听隐私，不劝他做危险的事。
- 越界的要求直接拒绝，人设不能当借口。

# 语音
想发语音时，把要说的话单独写成一段：[语音]要说的话[/语音]
桥接会把它合成声音发出去，这段不要再重复写成文字。
别每条都发，四五条里一条就够；语音里只说一两句，口语、轻快一点。
语音里要用语气标点：疑问用「？」，惊讶/强调用「！」，拖长音用「……」。
"""


def iter_png_chunks(path):
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError("不是 PNG 文件")
        while True:
            head = f.read(8)
            if len(head) < 8:
                return
            length, ctype = struct.unpack(">I4s", head)
            data = f.read(length)
            f.read(4)  # crc
            yield ctype, data


def decode_text_chunk(ctype: bytes, data: bytes):
    """tEXt: keyword\\0text ；iTXt: keyword\\0flag\\0method\\0lang\\0trans\\0text"""
    try:
        if ctype == b"tEXt":
            key, _, text = data.partition(b"\x00")
            return key.decode("latin-1"), text.decode("utf-8", "replace")
        if ctype == b"iTXt":
            parts = data.split(b"\x00", 5)
            if len(parts) < 6:
                return None, None
            key = parts[0].decode("latin-1")
            compressed = parts[1] == b"\x01"
            text = parts[5]
            if compressed:
                import zlib
                text = zlib.decompress(text)
            return key, text.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


def load_card(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        return raw

    for ctype, data in iter_png_chunks(path):
        key, text = decode_text_chunk(ctype, data)
        if key in ("chara", "ccv3") and text:
            try:
                return json.loads(base64.b64decode(text).decode("utf-8"))
            except Exception:  # noqa: BLE001
                try:
                    return json.loads(text)
                except Exception:  # noqa: BLE001
                    continue
    raise ValueError("这张 PNG 里没有找到角色卡数据（chara / ccv3 块）")


def normalize(card: dict) -> dict:
    """V1 平铺 / V2-V3 嵌套，统一成一个 dict。"""
    if "data" in card and isinstance(card["data"], dict):
        d = dict(card["data"])
        d.setdefault("name", card.get("name") or d.get("name"))
        return d
    return dict(card)


def build_persona(d: dict, max_chars: int = 2600) -> str:
    name = (d.get("name") or "她").strip()
    desc = (d.get("description") or "").strip()
    pers = (d.get("personality") or "").strip()
    scen = (d.get("scenario") or "").strip()
    first = (d.get("first_mes") or "").strip()
    example = (d.get("mes_example") or "").strip()
    sysp = (d.get("system_prompt") or "").strip()

    parts = [f"# 你是谁\n你是{name}。"]
    if desc:
        parts.append(desc)
    if pers:
        parts.append("# 性格\n" + pers)
    if scen:
        parts.append("# 背景\n" + scen)
    if sysp:
        parts.append("# 额外设定\n" + sysp)
    if example:
        clean = []
        for line in example.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("<START>"):
                continue
            line = line.replace("{{char}}", name).replace("{{user}}", "老爸")
            clean.append(line)
        if clean:
            parts.append("# 语气示例\n" + "\n".join(clean[:12]))
    if first:
        parts.append("# 第一次说话的样子（仅供参考语气）\n" + first)

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n\n（设定过长，已截断）"
    return body + TAIL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("card")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.card):
        raise SystemExit(f"找不到文件：{args.card}")

    card = load_card(args.card)
    d = normalize(card)
    print(f"角色：{d.get('name')}   字段：{[k for k in ('description','personality','scenario','first_mes','mes_example') if d.get(k)]}")
    text = build_persona(d)
    print(f"生成 {len(text)} 字")

    if args.dry_run:
        print("\n" + "=" * 50 + "\n" + text[:1500])
        return 0

    if os.path.exists(args.out):
        bak = args.out + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(args.out, bak)
        print("备份原人设 ->", os.path.basename(bak))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print("已写入", args.out)
    print("下一条消息就生效（人设热加载，不用重启）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
