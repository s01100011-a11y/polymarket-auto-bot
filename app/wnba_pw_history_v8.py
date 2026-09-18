from app import dashboard_filters_v5 as filters
from app import slack_ingest as ingest

filters.ingest = ingest

from app import wnba_pw_history_v7 as history

app = history.app
