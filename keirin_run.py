# 定時配信の入口。会場ごとに別メッセージで送る(送信数の上限が近いときは1通にまとめる)
# keirin_line.py の分析処理を使い、配信した買い目の保存(結果集計用)もここで行う
import calendar
import os
import time
from datetime import datetime, timezone, timedelta

import requests

import keirin_line as k
from keirin_track import save_picks

JST = timezone(timedelta(hours=9))
# 環境変数(GitHubのVariablesで設定。空のときは既定値)
SPLIT_MODE = os.environ.get("SPLIT_MODE") or "auto"           # auto:送信数を見て自動 / always:常に会場別 / off:1通にまとめる
PER_VENUE = int(os.environ.get("PER_VENUE") or 2)             # 会場ごとの本命・荒れの件数
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES") or 10)      # 1回の配信で送る最大通数
VENUES = [v.strip() for v in (os.environ.get("VENUES") or "").split(",") if v.strip()]

LINE_API = "https://api.line.me/v2/bot/message"


# ---------------- 送信数の管理 ----------------
def _auth():
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else None


def line_quota():
    """今月の送信数の上限と残りを調べる。戻り値: (種類, 残り)  種類 = unlimited / limited / unknown"""
    h = _auth()
    if not h:
        return "unlimited", None       # トークン未設定(テスト実行)
    try:
        q = requests.get(f"{LINE_API}/quota", headers=h, timeout=15).json()
        if q.get("type") != "limited":
            return "unlimited", None
        c = requests.get(f"{LINE_API}/quota/consumption", headers=h, timeout=15).json()
        return "limited", int(q.get("value", 0)) - int(c.get("totalUsage", 0))
    except Exception as e:
        print("送信数の確認に失敗:", e)
        return "unknown", None


def allowed_messages():
    """今回の配信で送ってよい通数。1なら『1通にまとめる』、0なら送らない"""
    if SPLIT_MODE == "off":
        return 1
    if SPLIT_MODE == "always":
        return MAX_MESSAGES
    kind, rem = line_quota()
    if kind == "unlimited":
        return MAX_MESSAGES
    if kind == "unknown":
        return 1
    now = datetime.now(JST)
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    deliveries_left = 3 * days_left          # 1日3回の配信
    reserve = 2 * days_left                  # 結果報告(1日最大2通)の分
    per = (rem - reserve) // deliveries_left
    print(f"今月の残り送信数 {rem}通 -> 1回あたり {per}通まで")
    if rem <= 0:
        return 0
    return max(1, min(MAX_MESSAGES, per))


def send_messages(texts):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN が未設定のため、送信せず表示のみ")
        for t in texts:
            print("-" * 30)
            print(t)
        return
    url = f"{LINE_API}/push" if user_id else f"{LINE_API}/broadcast"
    for i in range(0, len(texts), 5):        # 1回のAPI呼び出しは最大5通
        payload = {"messages": [{"type": "text", "text": t[:4900]} for t in texts[i:i + 5]]}
        if user_id:
            payload["to"] = user_id
        r = requests.post(url, headers={**_auth(), "Content-Type": "application/json"},
                          json=payload, timeout=20)
        print("LINE:", r.status_code, r.text[:200])
        r.raise_for_status()


# ---------------- 選手ページの取得(上位選手だけ) ----------------
def enrich_top(races, top=5):
    """候補レースの、評価上位の選手だけ直近成績を取る(時間の制限あり)"""
    start = time.time()
    for rc in races:
        rs = [r for r in rc["riders"] if r.get("score") is not None and r.get("pid")]
        for r in rs:
            k.rider_rating(r)
        rs.sort(key=lambda r: -r["rating"])
        for r in rs[:top]:
            pid = r["pid"]
            if pid not in k._FORM_CACHE:
                if time.time() - start > k.ENRICH_BUDGET:
                    print("選手ページの取得が制限時間に達したため、残りは基本データで続行")
                    return
                k._FORM_CACHE[pid] = k.fetch_player_form(pid)
                time.sleep(k.ENRICH_SLEEP)
            r["form"] = k._FORM_CACHE[pid]


# ---------------- 会場ごとのメッセージ ----------------
def venue_message(venue_jp, day, honmei, ara, level=3):
    jst = datetime.now(JST)
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点",
           f"📍{venue_jp}競輪" + (f" {day}日目" if day else ""), ""]
    if honmei:
        out.append(f"🎯【本命で決まりそう】{len(honmei)}レース")
        for i, x in enumerate(honmei, 1):
            out.append(k.race_block(x, "honmei", i, level))
        out.append("")
    if ara:
        out.append(f"🌪【荒れそう】{len(ara)}レース")
        for i, x in enumerate(ara, 1):
            out.append(k.race_block(x, "ara", i, level))
        out.append("")
    out.append("※評価=得点+直近成績などの補正。参考情報で、的中や回収を保証するものではありません。")
    msg = "\n".join(out)
    if len(msg) > 4900 and level > 0:
        return venue_message(venue_jp, day, honmei, ara, level - 1)
    return msg


def earliest_deadline(h, a):
    ds = [x["deadline"] for x in h + a if x.get("deadline") is not None]
    return min(ds) if ds else 9999


def main():
    results = k.fetch_all_today()
    print(f"取得レース数: {len(results)}")
    if VENUES:
        print(f"会場を絞り込み中: {VENUES}")   # 絞り込みは keirin_line.py 側(fetch_all_today)で行う
    if not results:
        print("レースを取得できなかったため送信しません")
        return

    allowed = allowed_messages()
    print(f"今回送れる通数: {allowed}")
    if allowed == 0:
        print("今月の送信数の上限に達しているため、送信しません")
        return

    groups = {}
    for rc in results:
        groups.setdefault(rc["venue"], []).append(rc)
    split = allowed >= 2 and len(groups) >= 2
    print("配信の形:", "会場ごとに別メッセージ" if split else "1通にまとめる")

    # 1段目: 基本データで候補を絞る
    cand = []
    if split:
        for v, rcs in groups.items():
            h, a = k.pick(rcs, only_upcoming=True, n=PER_VENUE + 1)
            keys = {(x["venue_key"], x["race"]) for x in h + a}
            cand += [rc for rc in rcs if (rc["venue"], rc["race"]) in keys]
    else:
        h, a = k.pick(results, only_upcoming=True, n=8)
        keys = {(x["venue_key"], x["race"]) for x in h + a}
        cand = [rc for rc in results if (rc["venue"], rc["race"]) in keys]
    print(f"候補 {len(cand)}レース")
    if k.ENRICH and cand:
        try:
            enrich_top(cand)
        except Exception as e:
            print("選手ページの取得でエラー。基本データだけで続行:", e)

    # 2段目: 直近成績も入れた評価で、最終の買い目を決める
    if split:
        venue_picks = []
        for v, rcs in groups.items():
            pool_v = [rc for rc in cand if rc["venue"] == v]
            h, a = k.pick(pool_v, only_upcoming=True, n=PER_VENUE)
            if h or a:
                venue_picks.append((v, rcs[0].get("day"), h, a))
        if not venue_picks:
            print("対象レースがないため送信しません")
            return
        venue_picks.sort(key=lambda t: earliest_deadline(t[2], t[3]))
        texts = []
        for v, day, h, a in venue_picks:
            name = h[0]["venue"] if h else a[0]["venue"]
            texts.append(venue_message(name, day, h, a))
        if len(texts) > allowed:                 # 通数が足りないときは、残りを最後の1通にまとめる
            rest = "\n\n".join(texts[allowed - 1:])
            texts = texts[:allowed - 1] + [rest[:4900]]
        honmei = [x for _, _, h, _ in venue_picks for x in h]
        ara = [x for _, _, _, a in venue_picks for x in a]
    else:
        honmei, ara = k.pick(cand, only_upcoming=True)
        if not honmei and not ara:
            print("対象レースがないため送信しません")
            return
        texts = [k.build_message(honmei, ara)]

    send_messages(texts)
    save_picks(honmei, ara, results)


if __name__ == "__main__":
    main()
