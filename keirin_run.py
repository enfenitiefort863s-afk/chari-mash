# 定時配信の入口。keirin_line.py の処理に、買い目の保存と会場の絞り込みを足したもの
import os

import keirin_line as k
from keirin_track import save_picks


def main():
    results = k.fetch_all_today()
    print(f"取得レース数: {len(results)}")

    # 見たい会場だけに絞る場合: 環境変数 VENUES に、会場名(ローマ字)をカンマ区切りで指定
    venues = [v.strip() for v in os.environ.get("VENUES", "").split(",") if v.strip()]
    if venues:
        results = [rc for rc in results if rc["venue"] in venues]
        print(f"会場を絞り込み: {venues} -> {len(results)}レース")
    if not results:
        print("レースを取得できなかったため送信しません")
        return

    pool = results
    if k.ENRICH:
        cand_h, cand_a = k.pick(results, only_upcoming=True, n=8)
        keys = {(x["venue_key"], x["race"]) for x in cand_h + cand_a}
        cand = [rc for rc in results if (rc["venue"], rc["race"]) in keys]
        print(f"候補 {len(cand)}レースの選手ページを取得します")
        try:
            k.enrich_riders(cand)
            pool = cand
        except Exception as e:
            print("選手ページの取得でエラー。基本データだけで続行:", e)

    honmei, ara = k.pick(pool, only_upcoming=True)
    if not honmei and not ara:
        print("対象レースがないため送信しません")
        return
    k.send_line(k.build_message(honmei, ara))
    save_picks(honmei, ara, results)


if __name__ == "__main__":
    main()
