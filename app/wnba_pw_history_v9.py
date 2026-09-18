from app import wnba_pw_history_v8 as base

app = base.app
history = base.history

try:
    summary = history.wnba_pw_summary()
    print(
        "WNBA_PW_DB_READY "
        f"alerts={summary.get('alerts')} "
        f"graded={summary.get('graded')} "
        f"wins={summary.get('wins')} "
        f"losses={summary.get('losses')} "
        f"staked={summary.get('total_staked_usdc')} "
        f"pnl={summary.get('pnl_usdc')} "
        f"roi={summary.get('roi_pct')} "
        f"missing_bk={summary.get('missing_bk_ml')}"
    )
except Exception as exc:
    print(f"WNBA_PW_DB_SUMMARY_ERROR {type(exc).__name__}: {exc}")
