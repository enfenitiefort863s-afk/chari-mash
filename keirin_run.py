# 定時配信の入口。
# LINEには「おすすめレース」の一覧だけを短く送り、展開予想・買い目などの詳細は
# ページ(GitHub Pages)に載せて、そのURLをLINEから案内する。
import os
import time
from concurrent.futures import ThreadPoolExecutor

import keirin_line as k
import keirin_web as kw
from keirin_line import send_line
from keirin_track import save_picks

VENUES = [v.strip() for v in (os.environ.get("VENUES") or "").split(",") if v.strip()]
SESSION = os.environ.get("SESSION") or "all"   # day:通常開催のみ / midnight:ミッドナイトのみ / all:両方
N_PICKS = int(os.environ.get("N_PICKS") or 5)  # 本命・荒れ、それぞれ何レース選ぶか


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


def apply_min_score(honmei, ara):
    """実際の的中率・回収率から学習したしきい値(あれば)で、自信の無いレースを間引く。
    しきい値未満のものを全部外すと0件になる場合は、念のため一番上のものだけ残す"""
    min_h = k.MIN_SCORE.get("honmei")
    min_a = k.MIN_SCORE.get("ara")
    if min_h is not None:
        kept = [x for x in honmei if x["honmei"] >= min_h]
        honmei = kept or honmei[:1]
    if min_a is not None:
        kept = [x for x in ara if x["ara"] >= min_a]
        ara = kept or ara[:1]
    return honmei, ara


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

    # 1段目: 基本データで候補を絞る
    cand_h, cand_a = k.pick(results, only_upcoming=True, n=N_PICKS + 2)
    keys = {(x["venue_key"], x["race"]) for x in cand_h + cand_a}
    cand = [rc for rc in results if (rc["venue"], rc["race"]) in keys]
    print(f"候補 {len(cand)}レース")
    if k.ENRICH and cand:
        try:
            enrich_top(cand)
        except Exception as e:
            print("選手ページの取得でエラー。基本データだけで続行:", e)

    # 2段目: 直近成績も入れた評価で、最終の「おすすめレース」を決める
    honmei, ara = k.pick(cand, only_upcoming=True, n=N_PICKS)
    honmei, ara = apply_min_score(honmei, ara)
    if not honmei and not ara:
        print("対象レースがないため送信しません")
        return

    url = k.page_url()
    full_text = k.build_message(honmei, ara, level=3, enforce_limit=False)   # ページ用(フル)
    short_text = k.build_short_message(honmei, ara, url)                    # LINE用(短縮)

    try:
        kw.publish(full_text, label="予想配信")
    except Exception as e:
        print("ページの更新に失敗(LINE配信は続けます):", e)
    try:
        send_line(short_text)
    except Exception as e:
        print("LINE送信に失敗しました(ページには反映済みです):", e)

    save_picks(honmei, ara, results)


if __name__ == "__main__":
    main()
