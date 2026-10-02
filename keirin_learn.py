# 配信時に保存した特徴量(data/learn/*.jsonl)と、実際のレース結果から
# 評価点の重み(data/model.json)を学習し直す。
# 考え方: 各特徴量について「1着になった選手の平均値」と「1着にならなかった選手の平均値」の
# 差を求め、その差が大きいほど重みを上げる、単純な統計的な学習(ロジスティック回帰の簡易版)。
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import keirin_web as kw
from keirin_line import BASE, LEARN_KEYS, PRIOR_WEIGHTS, VENUE_JP, WORKERS, fetch
from keirin_track import DATA_DIR, LEARN_DIR, parse_result, read_rows

MODEL_PATH = os.environ.get("MODEL_PATH") or os.path.join(DATA_DIR, "model.json")
MIN_RACES = 150          # これ未満なら、まだ重みを更新しない
LOOKBACK_DAYS = 60       # 学習に使う過去データの日数


def load_learn_rows(days=LOOKBACK_DAYS):
    if not os.path.isdir(LEARN_DIR):
        return []
    files = sorted(f for f in os.listdir(LEARN_DIR) if f.endswith(".jsonl"))[-days:]
    seen, out = set(), []
    for fn in files:
        with open(os.path.join(LEARN_DIR, fn), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row["k"] in seen:
                    continue
                seen.add(row["k"])
                out.append(row)
    return out


def fetch_result(row):
    """レースキー(venue-cup-day-race)から結果ページを取り、(1着の車番, 決まり手の文字) を返す"""
    try:
        venue, cup, day, race = row["k"].split("-", 3)
    except ValueError:
        return None, ""
    url = f"{BASE}/keirin/{venue}/raceresult/{cup}/{day}/{race}"
    html = fetch(url, retries=0)
    time.sleep(0.2)
    if not html:
        return None, ""
    res = parse_result(html)
    if not res:
        return None, ""
    first = next((car for car, o in res["orders"].items() if o == 1), None)
    return first, res.get("kimarite", "")


def learn_weights(keys, rows):
    """特徴量ごとに、1着車と非1着車の平均差を求め、初期値に足して新しい重みにする"""
    sums1 = {k: 0.0 for k in keys}
    sums0 = {k: 0.0 for k in keys}
    n1 = n0 = 0
    for row, first in rows:
        for car, score, feats in row["r"]:
            is1 = (car == first)
            for i, k in enumerate(keys):
                if is1:
                    sums1[k] += feats[i]
                else:
                    sums0[k] += feats[i]
            n1 += is1
            n0 += (not is1)
    if n1 < 20 or n0 < 20:
        return None
    weights = {}
    for k in keys:
        diff = sums1[k] / n1 - sums0[k] / n0     # 1着と非1着で、この特徴量がどれだけ違うか
        weights[k] = round(max(0.0, min(4.0, PRIOR_WEIGHTS[k] + diff * 3)), 2)
    return weights


# ---------------- 実際の的中率・回収率からの、しきい値の学習 ----------------
MIN_THRESHOLD_ROWS = 60     # これ未満なら、まだしきい値を更新しない
TARGET_ROI = 120             # この回収率(%)を下回るスコア帯は、配信から外す候補にする(さらに絞り込み強め)


def tune_thresholds():
    """results.csv(実際の的中・回収)から、本命/荒れそれぞれの
    『これ以上のスコアなら配信してよい』という下限値を求める。
    スコアが低い順に少しずつ切り捨てながら、残りの回収率が目標を超える一番緩い所を探す"""
    rows = [r for r in read_rows() if str(r.get("status")) == "ok" and r.get("score") not in (None, "")]
    out = {}
    for kind in ("honmei", "ara"):
        rs = sorted((r for r in rows if r["kind"] == kind), key=lambda r: float(r["score"]))
        if len(rs) < MIN_THRESHOLD_ROWS:
            continue
        best = None
        # スコアの低いほうから2割ずつ切り捨てて、回収率が目標を超えたら、そこを下限にする
        for cut in range(0, 9):
            keep = rs[int(len(rs) * cut / 10):]
            if len(keep) < MIN_THRESHOLD_ROWS // 2:
                break
            cost = sum(int(r["cost"] or 0) for r in keep)
            pay = sum(int(r["payout"] or 0) for r in keep)
            roi = (pay * 100 / cost) if cost else 0
            if roi >= TARGET_ROI:
                best = float(keep[0]["score"])
                break
        if best is not None:
            out[kind] = round(best, 2)
    return out


# ---------------- 会場ごとの決まり手(戦術)の集計 ----------------
KIMARITE_STATS_PATH = os.environ.get("KIMARITE_STATS_PATH") or os.path.join(DATA_DIR, "kimarite_stats.json")


def classify_kimarite(kimarite, pos_label):
    """決まり手の文字と、1着選手の並び予想での位置(先頭/番手/単騎)から、戦術を分類する。
    『番手捲り』『飛びつき』は、ここで初めて区別する(ウィンチケットの決まり手は4種類だけのため)"""
    k = (kimarite or "").strip()
    if not k:
        return "不明"
    if "逃" in k:
        return "逃げ"
    if "捲" in k:
        if pos_label == 2:
            return "番手捲り"
        if pos_label in (0, None):
            return "単騎捲り"
        return "先頭捲り"
    if "差" in k:
        if pos_label == 0:
            return "飛びつき差し"       # 単騎だった選手が差して連対(飛びつきの近似)
        return "差し"
    if "マ" in k:
        if pos_label == 0:
            return "飛びつきマーク"     # 単騎だった選手がマークで連対(飛びつきの近似)
        return "マーク"
    return "不明"


def update_kimarite_stats(rows, results):
    """会場ごとに、戦術(逃げ/番手捲り/飛びつき等)の件数を積み上げて保存する"""
    try:
        with open(KIMARITE_STATS_PATH, encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    added = 0
    for row, (first, kimarite) in zip(rows, results):
        if first is None:
            continue
        venue = row.get("v") or row["k"].split("-", 1)[0]
        pos_label = (row.get("pos") or {}).get(str(first), (row.get("pos") or {}).get(first))
        tactic = classify_kimarite(kimarite, pos_label)
        v = stats.setdefault(venue, {})
        v[tactic] = v.get(tactic, 0) + 1
        added += 1
    stats["_updated"] = datetime_now_str()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(KIMARITE_STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"決まり手の集計を更新しました({added}件追加)")
    try:
        display = {VENUE_JP.get(v, v): counts for v, counts in stats.items() if v != "_updated"}
        display["_updated"] = stats["_updated"]
        kw.publish_stats(display)
    except Exception as e:
        print("決まり手グラフのページ更新に失敗(集計自体は保存済み):", e)


def datetime_now_str():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


def main():
    rows = load_learn_rows()
    print(f"学習データ候補 {len(rows)}レース")
    men_rows, girls_rows = [], []
    results = []
    if rows:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(fetch_result, rows))
        for row, (first, kimarite) in zip(rows, results):
            if first is None:
                continue
            (girls_rows if row.get("g") else men_rows).append((row, first))
        print(f"結果が判明: 男子{len(men_rows)}レース / ガールズ{len(girls_rows)}レース")

    men_w = learn_weights(LEARN_KEYS, men_rows) if len(men_rows) >= MIN_RACES else None
    girls_w = learn_weights(LEARN_KEYS, girls_rows) if len(girls_rows) >= MIN_RACES else None
    if not men_w and not girls_w:
        print(f"重みの学習には{MIN_RACES}レース以上が必要です(まだ達していません)")

    # 既存のモデル(あれば)に、更新できた分だけ重ねる
    try:
        with open(MODEL_PATH, encoding="utf-8") as f:
            model = json.load(f)
    except Exception:
        model = {}
    if men_w or girls_w:
        model["n_races"] = len(men_rows) + len(girls_rows)
        model["men"] = men_w or model.get("men") or PRIOR_WEIGHTS
        model["girls"] = girls_w or model.get("girls") or PRIOR_WEIGHTS

    thresholds = tune_thresholds()
    if thresholds:
        model["min_score"] = thresholds
        print("スコアのしきい値を更新しました:", thresholds)
    else:
        print("しきい値の学習には、まだ判定済みのレースが足りません")

    if rows and results:
        update_kimarite_stats(rows, results)

    if not model:
        print("更新できるものがありませんでした")
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    print("重みを更新しました:", model)


if __name__ == "__main__":
    main()
