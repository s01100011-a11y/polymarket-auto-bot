from app import wnba_pw_strategy_test_v12 as base
from app import pw_research_sync
from app import pw_scalping_research
from app import pw_market_research
from app import pw_game_reconstruction
from app import pw_spread_backtest
from app import nfl_capper_ingest
from app import cfb_capper_preview

app = base.app
history = base.history
dashboard = base.dashboard
core = base.core
ingest = base.ingest

pw_research_sync.install(
    app=app,
    history=history,
    core=core,
    dashboard=dashboard,
)

pw_scalping_research.install(
    app=app,
    history=history,
    dashboard=dashboard,
)

pw_market_research.install(
    app=app,
    history=history,
    ingest=ingest,
    dashboard=dashboard,
    strategy=base,
)

pw_game_reconstruction.install(
    app=app,
    history=history,
    dashboard=dashboard,
    ingest=ingest,
)

pw_spread_backtest.install(
    history=history,
    ingest=ingest,
)

nfl_capper_ingest.install(
    app=app,
    dashboard=dashboard,
    core=core,
)

cfb_capper_preview.install(
    app=app,
    dashboard=dashboard,
    core=core,
)
