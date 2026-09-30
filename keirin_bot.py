# LINEに会場名(と、あればレース番号)を送ると、その場でレースを取得・分析して返信するbot。
# GitHub Actionsは「決まった時間に動く」だけなので、これは常時起動のRenderで動かす。
# 使い方の例: 「松山」「高知9R」「松山 9」
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import keirin_line as k

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

JST = timezone(timedelta(hours=9))

# 会場名(日本語表記・ローマ字どちらでも)から、内部の会場キーを引けるようにする
VENUE_ALIASES = {}
for key, jp in k.VENUE_JP.items():
    VENUE_ALIASES[jp] = key
    VENUE_ALIASES[key] = key

_CACHE = {"date": None}   # 同じ日に何度も出走表トップを取りに行かないための簡易キャッシュ


def parse_query(text):
    """メッセージから (会場キー, レース番号 or None) を取り出す。会場が分からなければ (None, None)"""
    text = text.strip()
    race_no = None
    m = re.search(r"(\d{1,2})\s*(?:R|レース)?$", text, re.I)
    if m and m.group(1):
        race_no = int(m.group(1))
        text = text[:m.start()].strip()
    for name, key in sorted(VENUE_ALIASES.items(), key=lambda x: -len(x[0])):
        if name and name in text:
            return key, race_no
    return None, race_no


def today_races(venue_key):
    today = datetime.now(JST).strftime("%Y%m%d")
    if _CACHE.get("date") != today:
        _CACHE.clear()
        _CACHE["date"] = today
    if venue_key not in _CACHE:
        _CACHE[venue_key] = [r for r in k.get_today_races() if r["venue"] == venue_key]
    return _CACHE[venue_key]


def answer_for(venue_key, race_no):
    venue_jp = k.VENUE_JP.get(venue_key, venue_key)
    races = today_races(venue_key)
    if not races:
        return f"{venue_jp}は、本日の開催が見つかりませんでした。"

    if race_no:
        info = next((r for r in races if r["race"] == race_no), None)
        if not info:
            return f"{venue_jp}{race_no}Rが見つかりませんでした。番号を確認してください。"
        targets = [info]
    else:
        # レース番号の指定が無ければ、締切が一番近い(まだ発売中の)レースを選ぶ
        with ThreadPoolExecutor(max_workers=4) as ex:
            datas = list(ex.map(k.parse_race, [r["url"] for r in races]))
        now = k.now_min_jst()
        cands = []
        for info, data in zip(races, datas):
            if not data:
                continue
            dl = k.deadline_min(data["title"])
            if k.is_upcoming(dl, now):
                cands.append((dl if dl is not None else 9999, info))
        if not cands:
            return f"{venue_jp}は、本日発売中のレースが見つかりませんでした(すべて締切済みかもしれません)。"
        cands.sort(key=lambda t: t[0])
        targets = [cands[0][1]]

    out = []
    for info in targets:
        data = k.parse_race(info["url"])
        if not data:
            continue
        data.update(venue=info["venue"], race=info["race"], day=info["day"], cup=info["cup"])
        x = k.analyze(data)
        if not x:
            continue
        out.append(k.race_block(x, "honmei", None, level=3))
    return "\n".join(out) if out else "このレースは選手データが少なく、分析できませんでした。"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    venue_key, race_no = parse_query(event.message.text)
    if not venue_key:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="会場名を含めて送ってください。例:「松山」「高知9R」"))
        return
    try:
        text = answer_for(venue_key, race_no)
    except Exception as e:
        text = f"取得中にエラーが起きました。もう一度試してください。({e})"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text[:4900]))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
