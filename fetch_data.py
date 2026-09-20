#!/usr/bin/env python3
"""
A股涨停复盘数据生成器

每个交易日收盘后运行一次,从东方财富(通过 AkShare)拉取四个股池:
  涨停股池 / 炸板股池 / 跌停股池 / 昨日涨停股池
计算封板率、炸板率、连板梯队、昨日涨停晋级率等指标,
写入 data/YYYY-MM-DD.json,并重建 data/index.json(供网页读取趋势)。

用法:
  python fetch_data.py                    # 抓取今天(北京时间)
  python fetch_data.py --date 2026-09-18  # 抓取指定日期
  python fetch_data.py --backfill 20      # 回补最近 20 个交易日
  python fetch_data.py --rebuild-index    # 只重建 index.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
TZ = ZoneInfo("Asia/Shanghai")

# 是否剔除 ST / *ST(涨跌幅限制 5%,通常不计入连板情绪统计)
EXCLUDE_ST = True

# ---------------------------------------------------------------------------
# 字段映射:东方财富中文列名 -> 输出 JSON 的英文键
# ---------------------------------------------------------------------------
_COMMON = {
    "代码": "code",
    "名称": "name",
    "涨跌幅": "pct",
    "最新价": "price",
    "成交额": "amount",
    "流通市值": "float_cap",
    "换手率": "turnover",
    "所属行业": "industry",
}
MAPS = {
    "zt": {
        **_COMMON,
        "封板资金": "seal_amount",
        "首次封板时间": "first_time",
        "最后封板时间": "last_time",
        "炸板次数": "open_times",
        "连板数": "boards",
        "涨停统计": "stat",
    },
    "zb": {
        **_COMMON,
        "首次封板时间": "first_time",
        "炸板次数": "open_times",
        "涨停统计": "stat",
        "振幅": "amplitude",
    },
    "dt": {
        **_COMMON,
        "封单资金": "seal_amount",
        "最后封板时间": "last_time",
        "连续跌停": "days",
        "开板次数": "open_times",
    },
    "prev": {
        **_COMMON,
        "昨日封板时间": "first_time",
        "昨日连板数": "prev_boards",
        "涨停统计": "stat",
    },
}
YI_FIELDS = ("amount", "float_cap", "seal_amount")  # 元 -> 亿元
INT_FIELDS = ("boards", "open_times", "prev_boards", "days")
FLOAT_FIELDS = ("pct", "price", "turnover", "amplitude")


# ---------------------------------------------------------------------------
# 纯函数:清洗与指标计算(不依赖网络,方便测试)
# ---------------------------------------------------------------------------
def market_of(code: str) -> str:
    if code.startswith("688") or code.startswith("689"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


def fmt_time(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if ":" in s:
        return s
    s = s.split(".")[0].zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def pct(a: int, b: int) -> float | None:
    return round(a / b * 100, 2) if b else None


def normalize(df: pd.DataFrame | None, kind: str) -> list[dict]:
    """把东方财富返回的 DataFrame 清洗成 list[dict]。"""
    if df is None or df.empty:
        return []
    mapping = MAPS[kind]
    df = df.rename(columns=mapping)
    keep = [c for c in df.columns if c in set(mapping.values())]
    df = df[keep].copy()
    if EXCLUDE_ST and "name" in df.columns:
        df = df[~df["name"].astype(str).str.contains("ST", na=False)]

    rows: list[dict] = []
    for rec in df.to_dict("records"):
        rec = {k: (None if pd.isna(v) else v) for k, v in rec.items()}
        code = str(rec.get("code", "")).zfill(6)
        rec["code"] = code
        rec["market"] = market_of(code)
        for k in YI_FIELDS:
            if rec.get(k) is not None:
                rec[k] = round(float(rec[k]) / 1e8, 2)
        for k in FLOAT_FIELDS:
            if rec.get(k) is not None:
                rec[k] = round(float(rec[k]), 2)
        for k in INT_FIELDS:
            if rec.get(k) is not None:
                rec[k] = int(rec[k])
        for k in ("first_time", "last_time"):
            if k in rec:
                rec[k] = fmt_time(rec[k])
        rows.append(rec)
    return rows


def build_day(date: str, zt_df, zb_df, dt_df, prev_df) -> dict:
    zt = normalize(zt_df, "zt")
    zb = normalize(zb_df, "zb")
    dt = normalize(dt_df, "dt")
    prev = normalize(prev_df, "prev")

    for r in zt:
        r["boards"] = r.get("boards") or 1
        # 09:25:00 集合竞价即封死且从未打开 -> 一字板
        r["one_word"] = r.get("first_time") == "09:25:00" and not r.get("open_times")
    zt.sort(key=lambda r: (-r["boards"], r.get("first_time") or "99:99:99"))

    n_zt, n_zb, n_dt = len(zt), len(zb), len(dt)

    # ---- 连板梯队 ----
    ladder: dict[int, list[dict]] = {}
    for r in zt:
        ladder.setdefault(r["boards"], []).append(r)
    ladder_out = [
        {"boards": lv, "count": len(rows), "stocks": rows}
        for lv, rows in sorted(ladder.items(), reverse=True)
    ]
    max_boards = max(ladder) if ladder else 0

    # ---- 昨日涨停 -> 今日表现 / 晋级率 ----
    today_codes = {r["code"] for r in zt}
    levels: dict[int, dict] = {}
    for r in prev:
        lv = r.get("prev_boards") or 1
        lv_d = levels.setdefault(lv, {"boards": lv, "count": 0, "promoted": 0, "pcts": []})
        lv_d["count"] += 1
        r["promoted"] = r["code"] in today_codes
        if r["promoted"]:
            lv_d["promoted"] += 1
        if r.get("pct") is not None:
            lv_d["pcts"].append(r["pct"])

    prev_levels = []
    for lv in sorted(levels, reverse=True):
        d = levels[lv]
        prev_levels.append(
            {
                "boards": lv,
                "count": d["count"],
                "promoted": d["promoted"],
                "rate": pct(d["promoted"], d["count"]),
                "avg_pct": round(sum(d["pcts"]) / len(d["pcts"]), 2) if d["pcts"] else None,
            }
        )
    prev_pcts = [r["pct"] for r in prev if r.get("pct") is not None]
    prev_promoted = sum(1 for r in prev if r["promoted"])

    # ---- 行业分布 ----
    industries = Counter(r.get("industry") or "其他" for r in zt)
    industries_out = [{"name": k, "count": v} for k, v in industries.most_common(12)]

    summary = {
        "limit_up": n_zt,
        "broken": n_zb,
        "limit_down": n_dt,
        # 封板率 = 涨停 / (涨停 + 炸板);炸板率 = 炸板 / (涨停 + 炸板)
        "seal_rate": pct(n_zt, n_zt + n_zb),
        "broken_rate": pct(n_zb, n_zt + n_zb),
        "first_board": sum(1 for r in zt if r["boards"] == 1),
        "multi_board": sum(1 for r in zt if r["boards"] >= 2),
        "one_word": sum(1 for r in zt if r["one_word"]),
        "max_boards": max_boards,
        "prev_limit_up": len(prev),
        "promoted": prev_promoted,
        "promotion_rate": pct(prev_promoted, len(prev)),
        "prev_avg_pct": round(sum(prev_pcts) / len(prev_pcts), 2) if prev_pcts else None,
    }

    return {
        "date": date,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "exclude_st": EXCLUDE_ST,
        "summary": summary,
        "ladder": ladder_out,
        "prev_levels": prev_levels,
        "industries": industries_out,
        "limit_up": zt,
        "broken": zb,
        "limit_down": dt,
        "prev": prev,
    }


# ---------------------------------------------------------------------------
# 网络:AkShare 抓取
# ---------------------------------------------------------------------------
def _retry(fn, *args, tries: int = 3, wait: float = 2.0, **kwargs):
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 网络/接口错误统一重试
            last = e
            time.sleep(wait * (i + 1))
    raise last  # type: ignore[misc]


def _pool(fn, date_s: str, name: str, required: bool) -> pd.DataFrame:
    try:
        return _retry(fn, date=date_s)
    except Exception as e:  # noqa: BLE001
        if required:
            raise RuntimeError(f"{name} 抓取失败: {e}") from e
        print(f"  [warn] {name} 抓取失败,按空处理: {e}", file=sys.stderr)
        return pd.DataFrame()


def fetch_pools(date: str):
    import akshare as ak

    s = date.replace("-", "")
    zt = _pool(ak.stock_zt_pool_em, s, "涨停股池", required=True)
    zb = _pool(ak.stock_zt_pool_zbgc_em, s, "炸板股池", required=True)
    dt = _pool(ak.stock_zt_pool_dtgc_em, s, "跌停股池", required=False)
    prev = _pool(ak.stock_zt_pool_previous_em, s, "昨日涨停股池", required=False)
    return zt, zb, dt, prev


def trade_dates() -> list[str]:
    import akshare as ak

    df = _retry(ak.tool_trade_date_hist_sina)
    return sorted(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not serializable: {type(o)}")


def save_day(day: dict) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{day['date']}.json"
    path.write_text(
        json.dumps(day, ensure_ascii=False, separators=(",", ":"), default=_json_default),
        encoding="utf-8",
    )
    return path


def rebuild_index() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    days = []
    for f in sorted(DATA_DIR.glob("20??-??-??.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        days.append({"date": d["date"], **d["summary"]})
    idx = {"updated": datetime.now(TZ).isoformat(timespec="seconds"), "days": days}
    (DATA_DIR / "index.json").write_text(
        json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"index.json: {len(days)} 个交易日")


# ---------------------------------------------------------------------------
def run_one(date: str) -> bool:
    print(f"[{date}] 抓取中…")
    zt, zb, dt, prev = fetch_pools(date)
    if zt is None or zt.empty:
        print(f"[{date}] 涨停股池为空,跳过(非交易日或数据尚未生成)")
        return False
    day = build_day(date, zt, zb, dt, prev)
    save_day(day)
    s = day["summary"]
    print(
        f"[{date}] 涨停 {s['limit_up']} 炸板 {s['broken']} 跌停 {s['limit_down']} "
        f"封板率 {s['seal_rate']}% 最高 {s['max_boards']} 板"
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD,默认今天(北京时间)")
    ap.add_argument("--backfill", type=int, default=0, help="回补最近 N 个交易日")
    ap.add_argument("--rebuild-index", action="store_true", help="只重建 index.json")
    args = ap.parse_args()

    if args.rebuild_index:
        rebuild_index()
        return 0

    now = datetime.now(TZ)
    end = args.date or now.strftime("%Y-%m-%d")

    try:
        cal = trade_dates()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 交易日历获取失败,改用股池是否为空判断: {e}", file=sys.stderr)
        cal = []

    if args.backfill:
        pool = [d for d in cal if d <= end] if cal else [end]
        targets = pool[-args.backfill :]
    else:
        targets = [end]

    if cal and not args.backfill and end not in cal:
        print(f"[{end}] 不是交易日,无需更新")
        rebuild_index()
        return 0

    if end == now.strftime("%Y-%m-%d") and now.hour < 15:
        print("[warn] 现在还没收盘,数据可能不完整", file=sys.stderr)

    ok = 0
    for d in targets:
        try:
            ok += run_one(d)
        except Exception as e:  # noqa: BLE001
            print(f"[{d}] 失败: {e}", file=sys.stderr)
        time.sleep(1)

    rebuild_index()
    return 0 if ok or not targets else 1


if __name__ == "__main__":
    sys.exit(main())
