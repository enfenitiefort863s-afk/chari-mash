 週次の詳しい分析レポート。溜まった結果記録(data/results.csv)を細かく集計して送る。
# 切り口: 券種別(2車単/3連単)・星別・会場別・決まり手別・軸の着順・先週との比較。
# 件数が少ない切り口は「参考」と明記して、数字のブレを鵜呑みにしないようにする。
# 実行: 日曜 21:00 JST のワークフロー(keirin-weekly.yml)から。手動実行も可。
from datetime import datetime, timedelta

from keirin_line import send_line
from keirin_track import JST, read_rows, roi_mark, stats

MIN_N = 3        # これ未満の区分は表示しない
REF_N = 10       # これ未満は「参考」扱い


def _d(s):
    return datetime.strptime(s, "%Y%m%d").date()


def _win(rows, days, now):
    """now から直近 days 日(当日含む)に入る行"""
    lo = now - timedelta(days=days - 1)
    return [r for r in rows if r.get("date") and lo <= _d(r["date"]) <= now]


def _prev_win(rows, days, now):
    """今週ウィンドウの直前 days 日ぶん"""
    hi = now - timedelta(days=days)
    lo = now - timedelta(days=days * 2 - 1)
    return [r for r in rows if r.get("date") and lo <= _d(r["date"]) <= hi]


def _pct(a, b):
    return f"{a * 100 / b:.0f}%" if b else "-"


def _roi(pay, cost):
    return pay * 100 / cost if cost else None


def _line(label, s, note_ref=True):
    """stats() の結果を1行に: ラベル n件 的中x% 回収y%"""
    if not s:
        return None
    roi = _roi(s["pay"], s["cost"])
    if roi is None:
        return None
    ref = "（参考）" if note_ref and s["n"] < REF_N else ""
    return (f"{roi_mark(roi)} {label}：{s['n']}件 的中{_pct(s['hit'], s['n'])} "
            f"回収{roi:.0f}%{ref}")


def _star_of(r):
    try:
        score = float(r.get("score") or 0)
    except ValueError:
        return 0
    return min(5, max(1, int(score // 4) + 1))   # keirin_line.stars と同じ刻み


def build_weekly_report(rows, now=None):
    now = now or datetime.now(JST).date()
    rows = [r for r in rows if str(r.get("status")) == "ok"]
    if not rows:
        return None
    week = _win(rows, 7, now)
    prev = _prev_win(rows, 7, now)
    if not week:
        return None
    w0 = now - timedelta(days=6)
    out = [f"📊 **週間分析レポート**（{w0.month}/{w0.day}〜{now.month}/{now.day}）", ""]

    sw, sp = stats(week), stats(prev)
    out.append("【今週サマリ】")
    if sw:
        roi = _roi(sw["pay"], sw["cost"])
        out.append(f"対象{sw['n']}件　2車単的中 {_pct(sw['hit'], sw['n'])}　"
                   f"回収 {roi:.0f}%{roi_mark(roi)}")
        if sw["t3_n"]:
            roi3 = _roi(sw["t3_pay"], sw["t3_cost"])
            out.append(f"3連単的中 {_pct(sw['t3_hit'], sw['t3_n'])}　回収 {roi3:.0f}%{roi_mark(roi3)}")
    if sp:
        roi_p = _roi(sp["pay"], sp["cost"])
        out.append(f"（先週：{sp['n']}件　的中 {_pct(sp['hit'], sp['n'])}　回収 {roi_p:.0f}%）")
    out.append("")

    star_lines = []
    for star in (5, 4, 3):
        if star > 3:
            rs = [r for r in week if _star_of(r) == star]
        else:
            rs = [r for r in week if _star_of(r) <= 3]
        if len(rs) >= MIN_N:
            ln = _line(f"★{star}" if star > 3 else "★3以下", stats(rs))
            if ln:
                star_lines.append(ln)
    if star_lines:
        out.append("【星別】")
        out += star_lines
        out.append("")

    by_v = {}
    for r in week:
        by_v.setdefault(r["venue"], []).append(r)
    v_stats = []
    for v, rs in by_v.items():
        if len(rs) >= MIN_N:
            s = stats(rs)
            v_stats.append((_roi(s["pay"], s["cost"]) or 0, v, s))
    if v_stats:
        v_stats.sort(reverse=True)
        out.append("【会場別（3件以上）】")
        if len(v_stats) <= 3:
            for i, (_, v, s) in enumerate(v_stats):
                out.append(_line(f"好調 {v}" if i == 0 else v, s))
        else:
            for _, v, s in v_stats[:2]:
                out.append(_line(f"好調 {v}", s))
            for _, v, s in v_stats[-2:]:
                out.append(_line(f"不調 {v}", s))
        out.append("")

    by_k = {}
    for r in week:
        by_k.setdefault(r.get("kimarite") or "不明", []).append(r)
    k_lines = []
    for name, rs in sorted(by_k.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= MIN_N:
            ln = _line(name, stats(rs))
            if ln:
                k_lines.append(ln)
    if k_lines:
        out.append("【決まり手別】")
        out += k_lines[:4]
        out.append("")

    fin = {}
    for r in week:
        f = str(r.get("axis_finish") or "?")
        fin[f] = fin.get(f, 0) + 1
    n = len(week)
    f1, f2, f3 = fin.get("1", 0), fin.get("2", 0), fin.get("3", 0)
    f_out = n - f1 - f2 - f3
    out.append("【軸の着順】")
    out.append(f"1着 {_pct(f1, n)} / 2着 {_pct(f2, n)} / 3着 {_pct(f3, n)} / "
               f"4着以下 {_pct(f_out, n)}（軸飛び）")
    out.append("")

    out.append("【累計】")
    for kind, label in (("honmei", "🎯本命"), ("ara", "🌪荒れ")):
        ln = _line(label, stats([r for r in rows if r["kind"] == kind]), note_ref=False)
        if ln:
            out.append(ln)
    all_s = stats(rows)
    if all_s:
        out.append(_line("合計", all_s, note_ref=False))
    return "\n".join(out)


def main():
    text = build_weekly_report(read_rows())
    if not text:
        print("対象データがないため送信しません")
        return
    send_line(text)
    print("週間分析レポートを送信しました")


if __name__ == "__main__":
    main()
