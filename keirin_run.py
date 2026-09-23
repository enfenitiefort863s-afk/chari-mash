# 定時配信の入口。会場ごとに別メッセージで送る(送信数の上限が近いときは1通にまとめる)
# keirin_line.py の分析処理を使い、配信した買い目の保存(結果集計用)もここで行う
import calendar
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from concurrent.futures import ThreadPoolExecutor

import json

import keirin_line as k
import keirin_web as kw
from keirin_track import save_picks, save_learn_rows

JST = timezone(timedelta(hours=9))
# 環境変数(GitHubのVariablesで設定。空のときは既定値)
SPLIT_MODE = os.environ.get("SPLIT_MODE") or "auto"           # auto:送信数を見て自動 / always:常に会場別 / off:1通にまとめる
PER_VENUE = int(os.environ.get("PER_VENUE") or 2)             # 会場ごとの本命・荒れの件数
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES") or 10)      # 1回の配信で送る最大通数
VENUES = [v.strip() for v in (os.environ.get("VENUES") or "").split(",") if v.strip()]
SESSION = os.environ.get("SESSION") or "all"   # day:通常開催のみ / midnight:ミッドナイトのみ / all:両方

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
    deliveries_left = 2 * days_left          # 1日2回の配信(朝8時・夜8時)
    reserve = 5 * days_left                  # 結果報告(1日2通)+的中速報(1日3通ぶんの余裕)
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
        payload = {"messages": [{"type": "text", "text": k.fit_text(t)} for t in texts[i:i + 5]]}
        if user_id:
            payload["to"] = user_id
        r = requests.post(url, headers={**_auth(), "Content-Type": "application/json"},
                          json=payload, timeout=20)
        print("LINE:", r.status_code, r.text[:200])
        r.raise_for_status()


# ---------------- 選手ページの取得(上位選手だけ) ----------------
def enrich_top(races, top=5):
    """候補レースの、評価上位の選手(と単騎の選手)だけ、選手ページを並行して取る(時間の制限あり)"""
    sel, need = [], []
    for rc in races:
        rs = [r for r in rc["riders"] if r.get("score") is not None and r.get("pid")]
        for r in rs:
            k.rider_rating(r)
        rs.sort(key=lambda r: -r["rating"])
        singles = {ln[0] for ln in (rc.get("lines") or []) if len(ln) == 1}   # 飛びつきの判断に必要
        for r in rs[:top] + [x for x in rs[top:] if x["車"] in singles]:
            sel.append(r)
            if r["pid"] not in k._FORM_CACHE and r["pid"] not in need:
                need.append(r["pid"])
    print(f"選手ページ {len(need)}人を取得します(同時 {k.WORKERS})")
    start = time.time()

    def work(pid):
        if time.time() - start > k.ENRICH_BUDGET:
            return
        k._FORM_CACHE[pid] = k.fetch_player_form(pid)
        time.sleep(k.FETCH_DELAY)

    with ThreadPoolExecutor(max_workers=k.WORKERS) as ex:
        list(ex.map(work, need))
    if time.time() - start > k.ENRICH_BUDGET:
        print("選手ページの取得が制限時間に達したため、一部は基本データで続行")
    for r in sel:
        pid = r["pid"]
        if pid in k._FORM_CACHE:
            r["form"] = k._FORM_CACHE[pid]
            r["rel"] = k._REL_CACHE.get(pid, {})


# ---------------- 会場ごとのメッセージ ----------------
def venue_message(venue_jp, day, honmei, ara, level=3):
    """1会場ぶんの配信文。おすすめレースの一覧 → レースごとの詳細"""
    jst = datetime.now(JST)
    picks = k.sort_picks(honmei, ara)
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点",
           f"📍{venue_jp}競輪" + (f" {day}日目" if day else ""), ""]
    out += k.pick_list(picks, False)
    for kind, x in picks:
        out.append(k.race_block(x, kind, None, level))
    out += ["", "", k.footer()]
    msg = "\n".join(out)
    if k.line_len(msg) > k.LINE_LIMIT and level > 0:
        return venue_message(venue_jp, day, honmei, ara, level - 1)
    return msg


def overflow_message(items):
    """入りきらない会場を、1通にコンパクトにまとめる(レースごとに買い目だけ)"""
    jst = datetime.now(JST)
    out = [f"📅 {jst.month}/{jst.day} {jst.hour}:{jst.minute:02d} 時点",
           "📍その他の会場(買い目のみ)"]
    for v, day, h, a in items:
        name = h[0]["venue"] if h else a[0]["venue"]
        out += ["", f"【{name}競輪】"]
        for kind, x in k.sort_picks(h, a):
            icon = "🎯" if kind == "honmei" else "🌪"
            axis = x[f"{kind}_axis"]
            who = (x["by_car"].get(axis) or {}).get("name", "")
            out.append(f"{icon}{x['race']}R ⏰{k.fmt_time(x['deadline'])} 軸{k.circ(axis)}{who}")
            out.append(f"　3連単 {k.tri_text(x[kind + '_tri'])}")
            out.append(f"　2車単 {k.circ(axis)}→{k.cs(x[kind + '_partners'])}")
    out += ["", "※参考情報です。的中や回収を保証するものではありません。"]
    return k.fit_text("\n".join(out))


def save_article(honmei, ara):
    """今回の予想を、note用の記事として data/articles/ に保存する"""
    from keirin_publish import build_article, save_article as _save
    by_venue = {}
    for kind, x in k.sort_picks(honmei, ara):
        by_venue.setdefault(x["venue"], []).append((kind, x))
    title, body = build_article(by_venue)
    _save(title, body)


def earliest_deadline(h, a):
    ds = [x["deadline"] for x in h + a if x.get("deadline") is not None]
    return min(ds) if ds else 9999


def main():
    results = k.fetch_all_today()
    print(f"取得レース数: {len(results)}")
    if VENUES:
        print(f"会場を絞り込み中: {VENUES}")   # 絞り込みは keirin_line.py 側(fetch_all_today)で行う
    if SESSION == "day":
        results = [rc for rc in results if not k.is_midnight_race(rc.get("title"))]
        print(f"通常開催のみに絞り込み -> {len(results)}レース")
    elif SESSION == "midnight":
        results = [rc for rc in results if k.is_midnight_race(rc.get("title"))]
        print(f"ミッドナイトのみに絞り込み -> {len(results)}レース")
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
        if len(texts) > allowed:                 # 通数が足りないときは、残りの会場を最後の1通にまとめる
            texts = texts[:allowed - 1] + [overflow_message(venue_picks[allowed - 1:])]
        honmei = [x for _, _, h, _ in venue_picks for x in h]
        ara = [x for _, _, _, a in venue_picks for x in a]
    else:
        honmei, ara = k.pick(cand, only_upcoming=True)
        if not honmei and not ara:
            print("対象レースがないため送信しません")
            return
        texts = [k.build_message(honmei, ara)]

    try:
        kw.publish(texts, label="予想配信")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_messages(texts)
    except Exception as e:
        print("LINE送信に失敗しました(ページには反映済みです):", e)
    save_picks(honmei, ara, results)
    try:
        save_learn_rows(results)                 # 学習用: 全レースの特徴量
        save_article(honmei, ara)                # note用: 今回の予想を記事の形にして保存
    except Exception as e:                       # 配信は済んでいるので、保存の失敗では止めない
        print("学習データ・記事の保存でエラー:", e)


if __name__ == "__main__":
    main()
