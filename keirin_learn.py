# 配信時に保存した特徴量(data/learn/*.jsonl)と、実際のレース結果から
# 評価点の重み(data/model.json)を学習し直す。
# 考え方: 各特徴量について「1着になった選手の平均値」と「1着にならなかった選手の平均値」の
# 差を求め、その差が大きいほど重みを上げる、単純な統計的な学習(ロジスティック回帰の簡易版)。
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from keirin_line import BASE, LEARN_KEYS, PRIOR_WEIGHTS, WORKERS, fetch
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


def fetch_finish(row):
    """レースキー(venue-cup-day-race)から結果ページを取り、1着の車番を返す"""
    try:
        venue, cup, day, race = row["k"].split("-", 3)
    except ValueError:
        return None
    url = f"{BASE}/keirin/{venue}/raceresult/{cup}/{day}/{race}"
    html = fetch(url, retries=0)
    time.sleep(0.2)
    if not html:
        return None
    res = parse_result(html)
    if not res:
        return None
    for car, o in res["orders"].items():
        if o == 1:
            return car
    return None


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
TARGET_ROI = 90              # この回収率(%)を下回るスコア帯は、配信から外す候補にする


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


def main():
    rows = load_learn_rows()
    print(f"学習データ候補 {len(rows)}レース")
    men_rows, girls_rows = [], []
    if rows:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            finishes = list(ex.map(fetch_finish, rows))
        for row, first in zip(rows, finishes):
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

    if not model:
        print("更新できるものがありませんでした")
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    print("重みを更新しました:", model)


if __name__ == "__main__":
    main()
