# 配信した買い目の保存と、レース結果の集計(的中率・回収率)
import csv
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup

from keirin_line import BASE, WORKERS, clean, fetch, to_int

JST = timezone(timedelta(hours=9))
DATA_DIR = os.environ.get("DATA_DIR", "data")
PICK_DIR = os.path.join(DATA_DIR, "picks")
RESULT_CSV = os.path.join(DATA_DIR, "results.csv")
FIELDS = ["pick_id", "date", "slot", "kind", "venue", "venue_key", "race",
          "axis", "partners", "axis_finish", "first", "second", "third",
          "kimarite", "hit", "cost", "payout", "status"]
KIND_LABEL = {"honmei": "🎯本命", "ara": "🌪荒れ"}


# ---------------- 配信した買い目の保存 ----------------
def save_picks(honmei, ara, results):
    """配信した本命・荒れの買い目を data/picks/YYYYMMDD.json に追記する"""
    url_of = {(rc["venue"], rc["race"]): rc.get("url", "") for rc in results}
    now = datetime.now(JST)
    picks = []
    for kind, xs in (("honmei", honmei), ("ara", ara)):
        for x in xs:
            m = re.search(r"/racecard/(\d+)/(\d+)/",
                          url_of.get((x["venue_key"], x["race"]), ""))
            if not m:
                continue
            if kind == "honmei":
                axis, partners, bet = x["honmei_axis"], x["honmei_partners"], x["honmei_bet"]
            else:
                axis, partners, bet = x["ara_axis"], x["ara_partners"], x["ara_bet"]
            picks.append({
                "kind": kind, "venue": x["venue"], "venue_key": x["venue_key"],
                "cup": m.group(1), "day": int(m.group(2)), "race": x["race"],
                "axis": axis, "partners": list(partners), "bet": bet,
            })
    if not picks:
        return
    os.makedirs(PICK_DIR, exist_ok=True)
    path = os.path.join(PICK_DIR, now.strftime("%Y%m%d") + ".json")
    entries = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append({"time": now.strftime("%H:%M"), "picks": picks})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    print(f"買い目を保存しました: {path} ({len(picks)}件)")


def load_picks(days=3):
    """直近days日ぶんの買い目を読む。同じ日・同じレース・同じ種類は最初の1件だけ"""
    if not os.path.isdir(PICK_DIR):
        return []
    files = sorted(f for f in os.listdir(PICK_DIR) if f.endswith(".json"))[-days:]
    out, seen = [], set()
    for fn in files:
        date = fn[:-5]
        try:
            with open(os.path.join(PICK_DIR, fn), encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            continue
        for e in entries:
            for p in e.get("picks", []):
                pid = f"{date}-{p['kind']}-{p['venue_key']}-{p['race']}"
                if pid in seen:
                    continue
                seen.add(pid)
                out.append({**p, "pick_id": pid, "date": date, "slot": e.get("time", "")})
    return out


# ---------------- 結果ページの読み取り ----------------
def parse_result(html):
    """結果ページから、着順・決まり手・払戻金を取る。まだ終わっていなければNone"""
    soup = BeautifulSoup(html, "html.parser")
    orders, kim, payouts = {}, "", {}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if not rows:
            continue
        head = [clean(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        if "選手名" in head and "着" in head:
            i_kim = head.index("決") if "決" in head else None
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue
                car, order = to_int(cells[1]), to_int(cells[0])
                if car is None:
                    continue
                orders[car] = order            # 落車・失格などはNone
                if order == 1 and i_kim is not None and i_kim < len(cells):
                    kim = cells[i_kim]
        elif "賭け式" in head and "払戻金" in head:
            cur = None
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) >= 4:
                    cur = cells[0] or cur
                    combo, amt = cells[1], cells[2]
                elif len(cells) == 3:          # ワイドなど(先頭セルが結合されている行)
                    combo, amt = cells[0], cells[1]
                else:
                    continue
                payouts.setdefault(cur, []).append(
                    (clean(combo), to_int(re.sub(r"[^\d]", "", amt))))
    if sum(1 for o in orders.values() if o) < 3:
        return None
    return {"orders": orders, "kimarite": kim, "payouts": payouts}


def evaluate(pick, res):
    """買い目(2車単: 軸→相手)が当たったか、払戻はいくらかを判定して1行にする"""
    orders = res["orders"]
    by_order = {o: c for c, o in orders.items() if o}
    axis, partners = pick["axis"], pick["partners"]
    first, second, third = by_order.get(1), by_order.get(2), by_order.get(3)
    hit = (first == axis and second in partners)
    cost = 100 * len(partners)
    payout = 0
    if hit:
        for combo, amt in res["payouts"].get("2車単", []):
            if combo == f"{axis}-{second}":
                payout = amt or 0
    return {
        "pick_id": pick["pick_id"], "date": pick["date"], "slot": pick["slot"],
        "kind": pick["kind"], "venue": pick["venue"], "venue_key": pick["venue_key"],
        "race": pick["race"], "axis": axis,
        "partners": "-".join(map(str, partners)),
        "axis_finish": orders.get(axis) or 0,
        "first": first or "", "second": second or "", "third": third or "",
        "kimarite": res["kimarite"], "hit": int(hit),
        "cost": cost, "payout": payout, "status": "ok",
    }


# ---------------- 結果の記録 ----------------
def read_rows():
    if not os.path.exists(RESULT_CSV):
        return []
    with open(RESULT_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_rows(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    new_file = not os.path.exists(RESULT_CSV)
    with open(RESULT_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def process_pending():
    """結果が出ているレースを判定してcsvに追記。まだのレースは次回に持ち越し"""
    done = {r["pick_id"] for r in read_rows()}
    limit = (datetime.now(JST) - timedelta(days=2)).strftime("%Y%m%d")
    pend = [p for p in load_picks(days=3) if p["pick_id"] not in done]

    def get(p):
        url = f"{BASE}/keirin/{p['venue_key']}/raceresult/{p['cup']}/{p['day']}/{p['race']}"
        html = fetch(url, retries=0)
        time.sleep(0.3)
        return parse_result(html) if html else None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:      # 結果ページを並行して取得
        parsed = list(ex.map(get, pend))

    new_rows = []
    for p, res in zip(pend, parsed):
        if res:
            new_rows.append(evaluate(p, res))
        elif p["date"] < limit:                # 2日たっても結果が取れない(中止など)
            new_rows.append({
                "pick_id": p["pick_id"], "date": p["date"], "slot": p["slot"],
                "kind": p["kind"], "venue": p["venue"], "venue_key": p["venue_key"],
                "race": p["race"], "axis": p["axis"],
                "partners": "-".join(map(str, p["partners"])),
                "axis_finish": 0, "first": "", "second": "", "third": "",
                "kimarite": "", "hit": 0, "cost": 0, "payout": 0, "status": "no_result",
            })
    if new_rows:
        append_rows(new_rows)
    return new_rows


# ---------------- 集計とLINEの報告文 ----------------
def stats(rows):
    rows = [r for r in rows if str(r.get("status")) == "ok"]
    if not rows:
        return None
    n = len(rows)
    return {
        "n": n,
        "win": sum(1 for r in rows if str(r["axis_finish"]) == "1"),
        "top3": sum(1 for r in rows if str(r["axis_finish"]) in ("1", "2", "3")),
        "hit": sum(1 for r in rows if str(r["hit"]) == "1"),
        "cost": sum(int(r["cost"] or 0) for r in rows),
        "pay": sum(int(r["payout"] or 0) for r in rows),
    }


def fmt_stats(label, s):
    def pct(a, b):
        return f"{a * 100 / b:.0f}%" if b else "-"
    roi = f"{s['pay'] * 100 / s['cost']:.0f}%" if s["cost"] else "-"
    return (f"{label} {s['n']}件\n"
            f"　軸1着 {s['win']}件({pct(s['win'], s['n'])}) / 3着内 {s['top3']}件\n"
            f"　2車単的中 {s['hit']}件({pct(s['hit'], s['n'])}) / 回収率 {roi}")


def build_report(new_rows):
    now = datetime.now(JST)
    out = [f"📊 結果報告 {now.month}/{now.day} {now.hour}:{now.minute:02d}", ""]
    out.append("【今回の結果】")
    for kind, label in KIND_LABEL.items():
        s = stats([r for r in new_rows if r["kind"] == kind])
        if s:
            out.append(fmt_stats(label, s))
    hits = [r for r in new_rows if str(r["hit"]) == "1"]
    if hits:
        out.append("✅的中: " + " / ".join(
            f"{r['venue']}{r['race']}R {int(r['payout']):,}円" for r in hits))
    all_rows = read_rows()
    out.append("")
    out.append("【累計】")
    for kind, label in KIND_LABEL.items():
        s = stats([r for r in all_rows if r["kind"] == kind])
        if s:
            out.append(fmt_stats(label, s))
    # 会場別(累計6件以上)
    by_v = {}
    for r in all_rows:
        by_v.setdefault(r["venue"], []).append(r)
    vlines = []
    for v, rs in by_v.items():
        s = stats(rs)
        if s and s["n"] >= 6:
            roi = s["pay"] * 100 / s["cost"] if s["cost"] else 0
            vlines.append((s["hit"] / s["n"], f"　{v} {s['n']}件 的中{s['hit'] * 100 / s['n']:.0f}% 回収{roi:.0f}%"))
    if vlines:
        out.append("")
        out.append("【会場別(6件以上)】")
        out += [t for _, t in sorted(vlines, reverse=True)[:8]]
    out.append("")
    out.append("※2車単・各100円で計算。参考情報です。")
    return "\n".join(out)
