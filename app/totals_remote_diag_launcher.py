from __future__ import annotations

import os

import uvicorn

from app import cfb_total_click_launcher as base
from app import pw_totals_diagnostics
from app import pw_totals_remote

app = base.app

pw_totals_remote.install(
    app=app,
    dashboard=base.composite.dashboard,
    core=base.composite.core,
)
pw_totals_diagnostics.start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
