from __future__ import annotations

import os

import uvicorn

# Install attribution/reconciliation guards before importing the dashboard
# chain. dashboard_attribution_v1 starts background services during import.
from app import sh01_attribution_reconcile_guard_v5 as _sh01_guard  # noqa: F401
from app import basketball_monitor_attribution_guard_v1 as _basketball_guard  # noqa: F401

# Keep the full dashboard/executor chain, promote SH01 into correctly scoped
# sport cards, then install the WNBA totals remote bridge on the same app.
from app import dashboard_sh01_capper_v3 as base
from app import more_stats_capper_sports_v1 as _capper_sport_stats  # noqa: F401
from app import audit_core_sync
from app import pw_totals_remote

app = base.app

pw_totals_remote.install(
    app=app,
    dashboard=base.dashboard,
    core=base.core,
)

audit_core_sync.install(
    app=app,
    core=base.core,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
