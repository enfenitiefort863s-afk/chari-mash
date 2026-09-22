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
          "axis", "axis_name", "partners", "axis_finish", "first", "second", "third",
          "kimarite", "hit", "cost", "payout", "status",
          "t3_hit", "t3_cost", "t3_payout"]
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
                "axis": axis, "axis_name": (x["by_car"].get(axis) or {}).get("name", ""),
                "partners": list(partners), "bet": bet,
                "tri": x.get(f"{kind}_tri"),
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


# ---------------- 学習用データの保存(配信時点の特徴量) ----------------
LEARN_DIR = os.path.join(DATA_DIR, "learn")


def save_learn_rows(results):
    """配信時点の全レースの特徴量を学習用に保存する(結果はあとで別途取得)"""
    from keirin_line import race_learn_row
    now = datetime.now(JST)
    path = os.path.join(LEARN_DIR, now.strftime("%Y%m%d") + ".jsonl")
    seen = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["k"])
                except Exception:
                    pass
    new_rows = []
    for rc in results:
        row = race_learn_row(rc)
        if row and row["k"] not in seen:
            seen.add(row["k"])
            new_rows.append(row)
    if not new_rows:
        return
    os.makedirs(LEARN_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"学習用データを保存しました: {path} ({len(new_rows)}レース)")


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
    # 3連単(軸→2着候補→3着候補のフォーメーション)
    tri = pick.get("tri")
    t3_cost, t3_hit, t3_pay = 0, 0, 0
    if tri:
        t3_cost = 100 * tri["points"]
        if first == tri["first"] and second in tri["second"] and third in tri["third"]:
            t3_hit = 1
            for combo, amt in res["payouts"].get("3連単", []):
                if combo == f"{first}-{second}-{third}":
                    t3_pay = amt or 0
    return {
        "pick_id": pick["pick_id"], "date": pick["date"], "slot": pick["slot"],
        "kind": pick["kind"], "venue": pick["venue"], "venue_key": pick["venue_key"],
        "race": pick["race"], "axis": axis, "axis_name": pick.get("axis_name", ""),
        "partners": "-".join(map(str, partners)),
        "axis_finish": orders.get(axis) or 0,
        "first": first or "", "second": second or "", "third": third or "",
        "kimarite": res["kimarite"], "hit": int(hit),
        "cost": cost, "payout": payout, "status": "ok",
        "t3_hit": t3_hit, "t3_cost": t3_cost, "t3_payout": t3_pay,
    }


# ---------------- 結果の記録 ----------------
def read_rows():
    if not os.path.exists(RESULT_CSV):
        return []
    with open(RESULT_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_rows(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(RESULT_CSV):
        with open(RESULT_CSV, encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:                   # 列が増えた(3連単など)ときは、古い行も含めて書き直す
            old = read_rows()
            with open(RESULT_CSV, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, restval="", extrasaction="ignore")
                w.writeheader()
                for r in old:
                    w.writerow(r)
    new_file = not os.path.exists(RESULT_CSV)
    with open(RESULT_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="", extrasaction="ignore")
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
                "race": p["race"], "axis": p["axis"], "axis_name": p.get("axis_name", ""),
                "partners": "-".join(map(str, p["partners"])),
                "axis_finish": 0, "first": "", "second": "", "third": "",
                "kimarite": "", "hit": 0, "cost": 0, "payout": 0, "status": "no_result",
                "t3_hit": 0, "t3_cost": 0, "t3_payout": 0,
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
    tri = [r for r in rows if int(r.get("t3_cost") or 0) > 0]     # 3連単の記録がある分だけ
    return {
        "n": n,
        "win": sum(1 for r in rows if str(r["axis_finish"]) == "1"),
        "top3": sum(1 for r in rows if str(r["axis_finish"]) in ("1", "2", "3")),
        "hit": sum(1 for r in rows if str(r["hit"]) == "1"),
        "cost": sum(int(r["cost"] or 0) for r in rows),
        "pay": sum(int(r["payout"] or 0) for r in rows),
        "t3_n": len(tri),
        "t3_hit": sum(1 for r in tri if str(r.get("t3_hit")) == "1"),
        "t3_cost": sum(int(r["t3_cost"] or 0) for r in tri),
        "t3_pay": sum(int(r.get("t3_payout") or 0) for r in tri),
    }


def fmt_stats(label, s):
    def pct(a, b):
        return f"{a * 100 / b:.0f}%" if b else "-"
    roi = f"{s['pay'] * 100 / s['cost']:.0f}%" if s["cost"] else "-"
    text = (f"{label} {s['n']}件\n"
            f"　軸1着 {s['win']}件({pct(s['win'], s['n'])}) / 3着内 {s['top3']}件\n"
            f"　2車単的中 {s['hit']}件({pct(s['hit'], s['n'])}) / 回収率 {roi}")
    if s["t3_n"]:
        roi3 = f"{s['t3_pay'] * 100 / s['t3_cost']:.0f}%" if s["t3_cost"] else "-"
        text += (f"\n　3連単的中 {s['t3_hit']}件({pct(s['t3_hit'], s['t3_n'])}) / 回収率 {roi3}"
                 f"(記録{s['t3_n']}件)")
    return text


def pickup_riders(rows, limit=3):
    """今回の配信ぶんから、良い結果を出した選手を選んで短い文にする"""
    ok = [r for r in rows if str(r.get("status")) == "ok" and r.get("axis_name")]
    cand = []
    for r in ok:
        fin = str(r.get("axis_finish"))
        if fin == "1":
            tag = "🥇軸的中" + (f"・払戻{int(r['payout']):,}円" if str(r.get("hit")) == "1" and int(r.get("payout") or 0) else "")
            score = 3 + (int(r.get("payout") or 0) / 1000)
        elif fin in ("2", "3"):
            tag = f"🥈{fin}着(僅差)"
            score = 1
        else:
            continue
        label = KIND_LABEL.get(r["kind"], "")
        cand.append((score, f"{r['venue']}{r['race']}R {r['axis_name']}（{label}）{tag}"))
    cand.sort(key=lambda t: -t[0])
    seen, out = set(), []
    for _, text in cand:
        name = text.split("　")[0] if "　" in text else text
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def report_date_str(now):
    """7:20台の実行は日付が変わったあとに前日ぶんを報告するため、その場合は前日の日付にする"""
    if now.hour < 10:
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


def build_alert(hits):
    """的中したレースだけを、判明した時点ですぐ知らせる短い文"""
    out = ["🔔的中速報！"]
    for r in hits:
        label = KIND_LABEL.get(r["kind"], "")
        who = r.get("axis_name") or f"{r['axis']}番"
        out.append(f"{r['venue']}{r['race']}R（{label}）")
        out.append(f"　◎{who} → {r['second']}番 的中")
        if int(r.get("payout") or 0):
            out.append(f"　払戻 {int(r['payout']):,}円(2車単)")
    out.append("")
    out.append("※1日の結果は、23:40ごろの結果報告でまとめてお知らせします。")
    return "\n".join(out)


def build_report(new_rows):
    now = datetime.now(JST)
    all_rows = read_rows()
    target_date = report_date_str(now)
    day_rows = [r for r in all_rows if r.get("date") == target_date]  # 速報ですでに記録済みの分も含む

    out = [f"📊 結果報告 {now.month}/{now.day} {now.hour}:{now.minute:02d}", ""]
    out.append("【本日の結果】" if day_rows else "【今回の結果】")
    src = day_rows or new_rows
    for kind, label in KIND_LABEL.items():
        s = stats([r for r in src if r["kind"] == kind])
        if s:
            out.append(fmt_stats(label, s))
    hits = [r for r in src if str(r["hit"]) == "1"]
    if hits:
        out.append("✅的中: " + " / ".join(
            f"{r['venue']}{r['race']}R {int(r['payout']):,}円" for r in hits))
    pu = pickup_riders(src)
    if pu:
        out.append("")
        out.append("🏆本日の好走ピックアップ")
        out += ["・" + t for t in pu]
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
    out.append("※2車単・3連単とも各100円で計算。参考情報です。")
    return "\n".join(out)
