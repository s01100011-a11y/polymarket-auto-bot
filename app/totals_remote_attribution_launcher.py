from __future__ import annotations

import os

import uvicorn

# Install attribution/reconciliation guards before importing the dashboard
# chain. dashboard_attribution_v1 starts background services during import.
from app import sh01_attribution_reconcile_guard_v5 as _sh01_guard  # noqa: F401
from app import basketball_monitor_attribution_guard_v1 as _basketball_guard  # noqa: F401

# Keep the full dashboard/executor chain, promote SH01 into correctly scoped
# sport cards with full Polymarket market metadata, then install the WNBA totals
# remote bridge on the same app.
from app import dashboard_sh01_capper_v4 as base
from app import ufc_sh01_dashboard as _ufc_sh01_dashboard  # noqa: F401
from app import newest_first_dashboard_v1 as _newest_first  # noqa: F401
from app import more_stats_capper_sports_v1 as _capper_sport_stats  # noqa: F401
from app import sh01_visible_position_stats_v1 as _sh01_visible_stats  # noqa: F401
from app import more_stats_sh01_truth_v1 as _sh01_more_stats_truth  # noqa: F401
from app import capper_section_toggles_v1 as _capper_section_toggles  # noqa: F401
from app import basketball_monitor_feed_box_v1 as _basketball_monitor_feed_box  # noqa: F401
from app import dashboard_taskbar_v3 as _dashboard_taskbar  # noqa: F401
from app import dashboard_ufc_v2 as _dashboard_ufc_v2  # noqa: F401
from app import ufc_fight_timing_v1 as _ufc_fight_timing  # noqa: F401
from app import ufc_price_stream_v1 as _ufc_price_stream  # noqa: F401
from app import ufc_fast_path_v1 as _ufc_fast_path  # noqa: F401
from app import ufc_disabled_fast_action_v1 as _ufc_disabled_fast_action  # noqa: F401
from app import ufc_position_summary_v1 as _ufc_position_summary  # noqa: F401
from app import executor_event_labels_v1 as _executor_event_labels  # noqa: F401
from app import ufc_live_page_v1 as _ufc_live_page  # noqa: F401
from app import ufc_live_sell_controls_v1 as _ufc_live_sell_controls  # noqa: F401
from app import ufc_rebuy_approval_v1 as _ufc_rebuy_approval  # noqa: F401
from app import ufc_live_overview_v1 as _ufc_live_overview  # noqa: F401
from app import ufc_live_activity_v1 as _ufc_live_activity  # noqa: F401
from app import ufc_current_capper_bets_v1 as _ufc_current_capper_bets  # noqa: F401
from app import ufc_main_dashboard_cleanup_v1 as _ufc_main_cleanup  # noqa: F401
from app import audit_core_sync
from app import audit_core_sync_schema_patch
from app import audit_research_store
from app import pw_totals_remote

app = base.app

pw_totals_remote.install(
    app=app,
    dashboard=base.dashboard,
    core=base.core,
)

audit_core_sync_schema_patch.install(audit_core_sync)
audit_core_sync.install(
    app=app,
    core=base.core,
)

audit_research_store.install(
    app=app,
    dashboard=base.dashboard,
    core=base.core,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))