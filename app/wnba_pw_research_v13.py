from app import wnba_pw_strategy_test_v12 as base
from app import pw_research_sync

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
