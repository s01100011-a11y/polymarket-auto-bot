from __future__ import annotations

import os

import uvicorn

# Install the universal attribution/reconciliation guard before importing the
# dashboard chain. dashboard_attribution_v1 starts the SH01 background watcher
# during import, so the guard must be loaded first.
from app import sh01_attribution_reconcile_guard_v4 as _sh01_guard  # noqa: F401

# Keep the full dashboard/executor chain, promote SH01 into the normal sport
# capper cards, then install the WNBA totals remote bridge on the same app.
from app import dashboard_sh01_capper_v2 as base
from app import pw_totals_remote

app = base.app

pw_totals_remote.install(
    app=app,
    dashboard=base.dashboard,
    core=base.core,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
