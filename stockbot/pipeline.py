"""流程主控：bootstrap（第一次）/ daily（每日收盤後）/ morning（隔日早盤修正）/ weekly（每週學習）。"""
from __future__ import annotations
from __future__ import annotations

import logging
import subprocess
import traceback
from datetime import datetime, timedelta

import pandas as pd

from . import analysis, collectors, config, db, evaluate, features, llm, memory_export, quant, report, rules
from .util import TPE, now_tpe, today_tpe

log = logging.getLogger(__name__)


def _start(conn, kind: str, run_date: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, run_date, started_at, status) VALUES (?,?,?, 'running')",
                       (kind, run_date, now_tpe().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


def _finish(conn, run_id: int, status: str, message: str = "") -> None:
    conn.execute("UPDATE runs SET finished_at=?, status=?, message=? WHERE id=?",
                 (now_tpe().isoformat(timespec="seconds"), status, message, run_id))
    conn.commit()


def _status_line(balance: dict) -> str:
    reason = llm.disabled_reason()
    if reason:
        return f"❌ LLM 已停止：{reason}"
    if balance.get("low"):
        return f"⚠️ {balance['message']}"
    return f"💰 {balance.get('message', '')}"


def _save_preds(conn, preds: list[dict]) -> None:
    now = now_tpe().isoformat(timespec="seconds")
    for p in preds:
        p["created_at"] = now
    db.upsert(conn, "predictions", preds)
    conn.commit()


def publish(message: str) -> None:
    """把報告與 memory/ 推上 GitHub（需設定 STOCKBOT_GIT_PUBLISH=1 與 git 推送權限）。"""
    if not config.GIT_PUBLISH:
        return
    cwd = str(config.BASE_DIR)
    paths = ["reports/stockbot", "memory"]     # 只 commit 自己負責的檔案，不碰其他已暫存的變更
    try:
        subprocess.run(["git", "add", "--", *paths], cwd=cwd, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet", "--", *paths], cwd=cwd).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message, "--", *paths], cwd=cwd, check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=cwd, check=True)
        subprocess.run(["git", "push"], cwd=cwd, check=True)
        log.info("已推送到 GitHub")
    except subprocess.CalledProcessError as e:
        log.error("git 推送失敗：%s", e)


def _universe_stale(conn) -> bool:
    row = conn.execute("SELECT MAX(updated_at) FROM stocks WHERE market!='INDEX'").fetchone()
    if not row or not row[0]:
        return True
    return datetime.fromisoformat(row[0]) < now_tpe() - timedelta(days=7)


# ════════════════════════════════════════
#  第一次安裝
# ════════════════════════════════════════

def bootstrap(chips_days: int = 60) -> None:
    with db.session() as conn:
        run = _start(conn, "bootstrap", today_tpe())
        balance = llm.check_balance()
        log.info(_status_line(balance))
        collectors.refresh_universe(conn)
        start = (now_tpe() - timedelta(days=365 * config.HISTORY_YEARS)).strftime("%Y-%m-%d")
        collectors.fetch_market(conn, start)
        stocks = [(r[0], r[1]) for r in conn.execute(
            "SELECT stock_id, market FROM stocks WHERE market!='INDEX' AND (theme IS NULL OR theme!='無')")]
        log.info("下載 %d 檔歷史行情（約需 10~20 分鐘）…", len(stocks))
        collectors.fetch_price_history(conn, stocks, start)
        collectors.fetch_fundamentals(conn)
        tdays = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM prices WHERE stock_id='^TWII' ORDER BY date DESC LIMIT ?", (chips_days,))]
        for i, d in enumerate(sorted(tdays)):
            log.info("回補三大法人 %s（%d/%d）", d, i + 1, len(tdays))
            collectors.fetch_chips(conn, d)
        collectors.fetch_news_cnyes(conn, since=now_tpe() - timedelta(days=7))
        feats = features.build_features(conn)
        quant.record_run(conn, "train", quant.train(feats))
        quant.record_run(conn, "calibrate", quant.calibrate(feats))
        rules.seed(conn, feats)
        memory_export.export(conn, full=True)
        _finish(conn, run, "ok", _status_line(balance))
    log.info("bootstrap 完成")


# ════════════════════════════════════════
#  每日（收盤後）
# ════════════════════════════════════════

def daily(force: bool = False) -> str | None:
    with db.session() as conn:
        run = _start(conn, "daily", today_tpe())
        try:
            balance = llm.check_balance()
            log.info(_status_line(balance))
            if _universe_stale(conn):
                collectors.refresh_universe(conn)

            trade_date = collectors.fetch_daily_quotes(conn)
            if not trade_date:
                _finish(conn, run, "fail", "抓不到收盤行情")
                return None
            done = conn.execute("SELECT 1 FROM runs WHERE kind='daily' AND run_date=? AND status='ok'",
                                (trade_date,)).fetchone()
            if done and not force:
                log.info("%s 已分析過，略過（--force 可重跑）", trade_date)
                _finish(conn, run, "skip", f"{trade_date} 已完成")
                return trade_date
            conn.execute("UPDATE runs SET run_date=? WHERE id=?", (trade_date, run))

            collectors.fetch_market(conn, (datetime.fromisoformat(trade_date) - timedelta(days=40)).strftime("%Y-%m-%d"))
            collectors.fetch_chips(conn, trade_date)
            collectors.fetch_fundamentals(conn)
            since = datetime.fromisoformat(trade_date).replace(tzinfo=TPE) - timedelta(days=2)
            collectors.fetch_news_cnyes(conn, since=since)
            names = dict(conn.execute("SELECT stock_id, name FROM stocks").fetchall())
            for theme_ids in config.CORE_WATCHLIST.values():
                for sid in theme_ids:
                    collectors.fetch_news_google(conn, sid, names.get(sid, theme_ids[sid]), since)

            n_eval = evaluate.evaluate_pending(conn)
            log.info("對帳 %d 筆預測", n_eval)

            feats = features.build_features(conn)
            day_all = feats[feats["date"] == trade_date]
            # 先對整個候選池算上漲機率（挑股要用），再選出今日名單
            pool = pd.concat([features.candidate_pool(feats, trade_date), features.core_today(feats, trade_date)])
            pool_preds = pd.DataFrame(quant.predict(pool))
            probs = pool_preds[pool_preds.variant == "quant"].set_index("stock_id")["prob_up"].to_dict() if not pool_preds.empty else {}
            universe = features.select_universe(conn, feats, trade_date, probs)
            log.info("今日名單：固定 %d + 動態 %d", (universe.source == "core").sum(), (universe.source == "dynamic").sum())

            for _, r in universe[universe.source == "dynamic"].iterrows():
                collectors.fetch_news_google(conn, r["stock_id"], names.get(r["stock_id"], ""), since)
            collectors.label_news(conn)

            analysis.attribute_day(conn, universe, trade_date)
            preds = quant.predict(universe)
            _save_preds(conn, preds)
            qdf = pd.DataFrame([p for p in preds if p["variant"] == "quant"])
            focus = analysis.pick_focus(universe, qdf, config.FOCUS_STOCKS_PER_DAY)
            llm_preds = analysis.analyst(conn, feats, universe, trade_date, focus, qdf)
            _save_preds(conn, llm_preds)

            md = report.daily(conn, trade_date, universe, focus, balance)
            (config.REPORT_DIR / "daily" / f"{trade_date}.md").write_text(md, encoding="utf-8")
            _finish(conn, run, "ok", _status_line(balance))
            memory_export.export(conn)
        except Exception as e:
            _finish(conn, run, "fail", f"{type(e).__name__}: {e}")
            log.error(traceback.format_exc())
            raise
    publish(f"stockbot: {trade_date} 日報與資料")
    return trade_date


# ════════════════════════════════════════
#  早盤修正（隔天 08:15）
# ════════════════════════════════════════

def morning() -> None:
    with db.session() as conn:
        row = conn.execute("SELECT MAX(date) FROM predictions WHERE variant='quant' AND date < ?",
                           (today_tpe(),)).fetchone()
        if not row or not row[0]:
            log.info("沒有可修正的預測")
            return
        d = row[0]
        run = _start(conn, "morning", d)
        collectors.fetch_market(conn, (datetime.fromisoformat(d) - timedelta(days=10)).strftime("%Y-%m-%d"))
        sox = db.query_df(conn, "SELECT date, close FROM prices WHERE stock_id='^SOX' AND date<=? "
                                "ORDER BY date DESC LIMIT 2", (d,))
        if len(sox) < 2 or sox.iloc[0]["date"] != d:
            sox_on = 0.0
            msg = f"美股 {d} 無交易，不修正"
        else:
            sox_on = float(sox.iloc[0]["close"] / sox.iloc[1]["close"] - 1)
            msg = f"費半 {d} {sox_on:+.2%}"
        q = db.query_df(conn, "SELECT * FROM predictions WHERE variant='quant' AND date=?", (d,))
        _save_preds(conn, quant.morning_adjust(q, sox_on))
        _finish(conn, run, "ok", msg)
        log.info("早盤修正完成：%s", msg)


# ════════════════════════════════════════
#  每週學習（週六）
# ════════════════════════════════════════

def weekly() -> None:
    today = now_tpe().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()   # 本週一
    if today.weekday() >= 5:
        week_end = (today - timedelta(days=today.weekday() - 4)).isoformat()
    else:  # 平日手動執行 → 檢討上週
        week_start = (today - timedelta(days=today.weekday() + 7)).isoformat()
        week_end = (today - timedelta(days=today.weekday() + 3)).isoformat()
    with db.session() as conn:
        run = _start(conn, "weekly", week_end)
        balance = llm.check_balance()
        collectors.refresh_universe(conn)
        feats = features.build_features(conn)
        metrics = quant.train(feats)
        quant.record_run(conn, "train", metrics)
        calib = quant.calibrate(feats)
        quant.record_run(conn, "calibrate", calib)
        if not conn.execute("SELECT 1 FROM rules LIMIT 1").fetchone():
            rules.seed(conn, feats)
        changes = rules.review_all(conn, feats)
        new_ids = rules.propose_from_mistakes(conn, feats, analysis.weekly_mistakes(conn, week_start))
        md = report.weekly(conn, week_start, week_end, changes, new_ids, metrics, calib, balance)
        iso = datetime.fromisoformat(week_start).isocalendar()
        (config.REPORT_DIR / "weekly" / f"{iso[0]}-W{iso[1]:02d}.md").write_text(md, encoding="utf-8")
        _finish(conn, run, "ok", _status_line(balance))
        memory_export.export(conn)
    publish(f"stockbot: {week_start} 週報、模型與規則更新")
