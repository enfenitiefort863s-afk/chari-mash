# 配信時に保存した特徴量(data/learn/*.jsonl)と、実際のレース結果から
# 評価点の重み(data/model.json)を学習し直す。
# 考え方: 各特徴量について「1着になった選手の平均値」と「1着にならなかった選手の平均値」の
# 差を求め、その差が大きいほど重みを上げる、単純な統計的な学習(ロジスティック回帰の簡易版)。
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from keirin_line import BASE, LEARN_KEYS, PRIOR_WEIGHTS, WORKERS, fetch
from keirin_track import DATA_DIR, LEARN_DIR, parse_result

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


def main():
    rows = load_learn_rows()
    print(f"学習データ候補 {len(rows)}レース")
    if not rows:
        return

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        finishes = list(ex.map(fetch_finish, rows))

    men_rows, girls_rows = [], []
    for row, first in zip(rows, finishes):
        if first is None:
            continue
        (girls_rows if row.get("g") else men_rows).append((row, first))
    print(f"結果が判明: 男子{len(men_rows)}レース / ガールズ{len(girls_rows)}レース")

    men_w = learn_weights(LEARN_KEYS, men_rows) if len(men_rows) >= MIN_RACES else None
    girls_w = learn_weights(LEARN_KEYS, girls_rows) if len(girls_rows) >= MIN_RACES else None
    if not men_w and not girls_w:
        print(f"学習には{MIN_RACES}レース以上が必要です(まだ達していません)")
        return

    model = {
        "n_races": len(men_rows) + len(girls_rows),
        "men": men_w or PRIOR_WEIGHTS,
        "girls": girls_w or PRIOR_WEIGHTS,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    print("重みを更新しました:", model)


if __name__ == "__main__":
    main()
