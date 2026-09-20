"""
outcomes.py — Game history storage and condition outcome analysis.

Data flow:
  monitor.py  →  append_game_record()  →  game_history.json
  monitor.py  →  write_live_analysis() →  live_analysis.json
  server.py   →  compute_outcomes()    →  /api/outcomes
  server.py   →  /api/history, /api/live-analysis
"""

import json
import hashlib
import math
import os
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations

try:
    import ml_model
except Exception:  # pragma: no cover - optional Phase 3A dependency path
    ml_model = None

import league_config

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE      = league_config.state_path("game_history.json")
LIVE_ANALYSIS_FILE = league_config.state_path("live_analysis.json")
BAYES_STATE_FILE  = league_config.state_path("bayes_state.json")
EDGE_STATE_FILE   = league_config.state_path("edge_state.json")
CONFIG_FILE       = league_config.CONFIG_FILE
OUTCOMES_CACHE_FILE = league_config.state_path("outcomes_cache.json")
OUTCOMES_CACHE_DIR = SCRIPT_DIR  # per-segment cache files live here
WAL_FILE = league_config.state_path("game_history.wal")

MIN_SAMPLE = 3   # minimum games for a stat to be shown
OUTCOMES_CACHE_TTL = 300  # seconds, configurable via config.json outcomes_cache_ttl
_OUTCOMES_SCHEMA_VERSION = 3  # bump when cache output schema changes to trigger background recompute
HISTORY_SCHEMA_VERSION = "game_history.v2"
LEGACY_CONFIG_FINGERPRINT = "legacy-unknown"
LIVE_SOURCE = "live"
BACKFILL_SOURCE = "backfill"
BAYES_STATE_VERSION = 1
_DEFAULT_BAYES_PRIORS = {
    "game_upset":    {"alpha": 7.0, "beta": 13.0},
    "game_close":    {"alpha": 8.0, "beta": 12.0},
    "team_wins":     {"alpha": 13.0, "beta": 7.0},
    "game_comeback": {"alpha": 3.0, "beta": 7.0},
}
BAYES_PRIORS = {k: dict(v) for k, v in _DEFAULT_BAYES_PRIORS.items()}


def apply_bayes_priors_from_config(config):
    """Override BAYES_PRIORS from config['bayes_priors'] if present.

    Config format: {"bayes_priors": {"game_upset": {"alpha": 5, "beta": 15}, ...}}
    Only overrides keys that exist in the defaults; missing keys keep defaults.
    """
    cfg_priors = (config or {}).get("bayes_priors")
    if not isinstance(cfg_priors, dict):
        return
    for key in _DEFAULT_BAYES_PRIORS:
        entry = cfg_priors.get(key)
        if isinstance(entry, dict):
            a = entry.get("alpha")
            b = entry.get("beta")
            if a is not None and b is not None:
                try:
                    BAYES_PRIORS[key] = {"alpha": float(a), "beta": float(b)}
                except (TypeError, ValueError):
                    pass


# Apply config-based priors at module load time
try:
    with open(league_config.CONFIG_FILE, encoding="utf-8") as _f:
        apply_bayes_priors_from_config(json.load(_f))
except Exception:
    pass

# ── Condition polarity classification (issue #152) ──────────────────────────
# Historical team_wins% when condition fires for that team:
#   <45% = NEGATIVE, >55% = POSITIVE, else NEUTRAL.
# Used by predicted winner filters to suppress overconfident small-sample calls.
_NEGATIVE_CONDITIONS = frozenset({
    "Critical turnovers (>25)",
    "Critical turnover rate (>20%)",
    "FG% <40% 1H",
    "FG% <40% 2H",
    "High turnover rate (>12.5%)",
    "High turnover rate (>15%)",
    "High turnovers (>15)",
    "Losing team FG% hot streak",
    "Points off TOs (>15)",
    "Points off TOs (>20)",
    "Q4 - COMEBACK ALERT!",
    "Trailing in 1st by 8+",
    "Trailing in 3rd by <12",
    "Trailing in 4th by <10",
    "Weak away 1H ML team",
})
_POSITIVE_CONDITIONS = frozenset({
    "FG% >50% 1H",
    "FG% >50% 2H",
    "Q2 MOMENTUM +5",
    "Q3 MOMENTUM +5",
    "Q4 MOMENTUM +5",
    "Strong away 1H ML team",
    "Strong home 1H ML team",
    "Winning Q2 by 5",
    "Winning Q3 by 6+",
    "Winning Q4 by 5+",
})

# Pattern-based polarity: substrings that classify conditions not in the exact-match sets.
# Checked case-insensitively. First match wins.
_NEGATIVE_PATTERNS = ("trailing", "turnover", "points off to", "slump", "fg% below",
                      "fg% <", "comeback", "losing team", "weak")
_POSITIVE_PATTERNS = ("winning q", "momentum", "hot shooting", "fg% >", "strong",
                      "unanswered points")


def classify_polarity(cond_name):
    """Return 'pos', 'neg', or None for a condition name."""
    if cond_name in _NEGATIVE_CONDITIONS:
        return "neg"
    if cond_name in _POSITIVE_CONDITIONS:
        return "pos"
    lower = cond_name.lower()
    for pat in _NEGATIVE_PATTERNS:
        if pat in lower:
            return "neg"
    for pat in _POSITIVE_PATTERNS:
        if pat in lower:
            return "pos"
    return None


# ── Condition edge store (issue #217) ─────────────────────────────────────────
# Rolling per-condition team_won rate.  Used by the PW edge gate to suppress
# calls where the average condition edge (oriented toward predicted team) is
# below a configurable threshold.

class ConditionEdgeStore:
    """Per-condition {fires, team_won} counters for rolling edge computation."""

    def __init__(self, conditions=None):
        # {cond_name: [fires, won]}
        self.conditions = conditions or {}

    @staticmethod
    def _is_end_of_game_artifact(cf):
        """Skip backfill entries evaluated at end-of-game with no mid-game timing (#258)."""
        qep = cf.get("quarter_elapsed_pct")
        if qep is None or qep != 100.0:
            return False
        # Has precise game_time_seconds → not an artifact (e.g. actual last-second fire)
        if cf.get("game_time_seconds") is not None:
            return False
        # time_anchor explicitly shows it was timed from PBP → keep it
        anchor = cf.get("time_anchor", "")
        if anchor in ("play", "pbp_play", "pbp_tick"):
            return False
        return True

    def update_from_record(self, record):
        if not isinstance(record, dict):
            return
        winner_id = str(record.get("winner_id") or "").strip()
        if not winner_id:
            return
        seen = set()
        for cf in record.get("conditions_fired") or []:
            if not isinstance(cf, dict):
                continue
            if self._is_end_of_game_artifact(cf):
                continue
            name = str(cf.get("name") or "").strip()
            tid = str(cf.get("team_id") or "").strip()
            if not name or not tid:
                continue
            key = (name, tid, str(record.get("game_id", "")))
            if key in seen:
                continue
            seen.add(key)
            if name not in self.conditions:
                self.conditions[name] = [0, 0]
            self.conditions[name][0] += 1
            tw = cf.get("team_won")
            if tw is None:
                tw = (tid == winner_id)
            if tw:
                self.conditions[name][1] += 1

    def edge(self, cond_name, min_fires=20):
        """Return team-win edge (pct - 50) for a condition, or 0 if insufficient data."""
        s = self.conditions.get(str(cond_name or "").strip())
        if not s or s[0] < min_fires:
            return 0.0
        return (s[1] / s[0] * 100.0) - 50.0

    def compute_avg_edge(self, active_conditions, pred_team_id, cond_team_map):
        """Compute average edge across conditions, oriented toward predicted team.

        cond_team_map: {cond_name: team_id} mapping conditions to the team they fired for.
        Returns avg_edge float or None if no conditions.
        """
        edges = []
        for cond in active_conditions:
            cn = str(cond.get("name") or "").strip() if isinstance(cond, dict) else str(cond or "").strip()
            if not cn:
                continue
            e = self.edge(cn)
            ct = cond_team_map.get(cn, "")
            if ct == pred_team_id:
                edges.append(e)
            else:
                edges.append(-e)
        return round(sum(edges) / len(edges), 2) if edges else None

    def save(self):
        tmp = EDGE_STATE_FILE + ".tmp"
        payload = {
            "version": 1,
            "history_fingerprint": list(_history_fingerprint()),
            "conditions": self.conditions,
        }
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, EDGE_STATE_FILE)

    @classmethod
    def rebuild_from_history(cls, records=None, save=True):
        store = cls()
        for record in (records if records is not None else load_history(migrate=True)):
            store.update_from_record(record)
        if save:
            store.save()
        return store

    @classmethod
    def load_or_rebuild(cls, records=None):
        try:
            with open(EDGE_STATE_FILE) as f:
                payload = json.load(f)
            if payload.get("version") != 1:
                raise ValueError("Unsupported edge cache version")
            fingerprint = payload.get("history_fingerprint")
            if tuple(fingerprint or ()) != _history_fingerprint():
                raise ValueError("Stale edge cache")
            return cls(conditions=payload.get("conditions") or {})
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return cls.rebuild_from_history(records=records, save=True)


# Per-segment cache: segment_key -> {fingerprint, min_sample, decay_lambda, max_combo_size, last_computed_at, no_changes_since, result}
_OUTCOMES_CACHE = {}
_OUTCOMES_RECOMPUTE_LOCK = threading.Lock()


def _outcomes_cache_key(segment, spread_role="", season_year="", margin_at_fire=""):
    """Build a composite cache key from segment + spread_role + season_year + margin_at_fire (#144, #153)."""
    parts = [segment or ""]
    if spread_role:
        parts.append("role=" + spread_role)
    if season_year:
        parts.append("yr=" + season_year)
    if margin_at_fire:
        parts.append("maf=" + margin_at_fire)
    return "|".join(parts) if len(parts) > 1 else parts[0]


def _cache_slot(segment_key):
    """Get or create a cache slot for a given segment key."""
    if segment_key not in _OUTCOMES_CACHE:
        _OUTCOMES_CACHE[segment_key] = {
            "fingerprint": None,
            "min_sample": None,
            "decay_lambda": None,
            "max_combo_size": None,
            "schema_version": None,
            "last_computed_at": None,
            "no_changes_since": None,
            "result": None,
        }
    return _OUTCOMES_CACHE[segment_key]


def _segment_cache_file(segment_key):
    """Return disk cache path for a given cache key."""
    safe = (segment_key or "all").replace("/", "_").replace("|", "_").replace("=", "_")
    if not safe:
        safe = "all"
    return os.path.join(OUTCOMES_CACHE_DIR, "outcomes_cache_{safe}_{league}.json".format(
        safe=safe, league=league_config.LEAGUE))


def _history_file_has_data():
    """Return True if game_history.json exists and is non-trivially sized (>100 bytes)."""
    try:
        return os.path.getsize(HISTORY_FILE) > 100
    except OSError:
        return False


# ── History I/O ───────────────────────────────────────────────────────────────

def _save_outcomes_cache_to_disk(segment_key=""):
    """Save a segment's cache slot to disk (atomic: temp file + rename)."""
    try:
        slot = _OUTCOMES_CACHE.get(segment_key)
        if not slot or slot.get("result") is None:
            return

        cache_data = {
            "fingerprint": slot["fingerprint"],
            "min_sample": slot["min_sample"],
            "decay_lambda": slot["decay_lambda"],
            "max_combo_size": slot["max_combo_size"],
            "schema_version": _OUTCOMES_SCHEMA_VERSION,
            "last_computed_at": slot["last_computed_at"],
            "result": slot["result"],
        }

        path = _segment_cache_file(segment_key)
        temp_path = path + ".tmp"
        with open(temp_path, 'w') as f:
            json.dump(cache_data, f)
        os.rename(temp_path, path)
    except Exception:
        pass


def _load_outcomes_cache_from_disk(segment_key=""):
    """Load a segment's cache from disk, even if fingerprint is stale.

    Always loads the cached result into the in-memory slot so compute_outcomes()
    can return stale data immediately while triggering a background recompute.
    The caller checks fingerprint match and spawns BG refresh as needed.
    """
    try:
        path = _segment_cache_file(segment_key)
        if not os.path.exists(path):
            return False

        with open(path) as f:
            cache_data = json.load(f)

        slot = _cache_slot(segment_key)
        slot["fingerprint"] = tuple(cache_data.get("fingerprint") or ())
        slot["min_sample"] = cache_data["min_sample"]
        slot["decay_lambda"] = cache_data["decay_lambda"]
        slot["max_combo_size"] = cache_data["max_combo_size"]
        slot["schema_version"] = cache_data.get("schema_version")
        slot["last_computed_at"] = cache_data["last_computed_at"]
        slot["result"] = cache_data["result"]
        return True
    except Exception:
        pass
    return False


def _clear_outcomes_cache():
    """Clear in-memory cache slots.

    Disk cache files are intentionally preserved — they serve as stale-but-usable
    fallbacks for the server process after monitor.py updates game_history.json.
    The fingerprint-based invalidation in compute_outcomes() detects stale caches
    and triggers background recomputation while returning stale data immediately,
    avoiding multi-minute cold-start hangs on /api/outcomes.
    """
    _OUTCOMES_CACHE.clear()
    # Legacy single-file cache tmp cleanup only
    try:
        tmp = OUTCOMES_CACHE_FILE + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
            pass


def _to_int_or_none(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- ROI helpers (issue #143) ---------------------------------------------------

FLAT_JUICE_ML = -110
FLAT_WIN_PROFIT = 100.0 * (100.0 / abs(FLAT_JUICE_ML))   # $90.91 on $100 wager
STAKE = 100.0


def _parse_moneyline(ml_str):
    """Parse American moneyline string (e.g. '+150', '-180') → int, or None."""
    if ml_str is None:
        return None
    s = str(ml_str).strip()
    if not s or s == "--":
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _ml_win_profit(ml_int):
    """Profit on a $100 winning bet at the given moneyline. Returns float."""
    if ml_int is None or ml_int == 0:
        return FLAT_WIN_PROFIT  # fallback
    if ml_int > 0:
        return STAKE * (ml_int / 100.0)
    return STAKE * (100.0 / abs(ml_int))


def _now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _normalize_bool_or_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("", "none", "null"):
            return None
        if raw in ("true", "1", "yes", "y"):
            return True
        if raw in ("false", "0", "no", "n"):
            return False
    return None


def _normalize_string_list(values):
    if not isinstance(values, list):
        return []
    out = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_pregame_tags(values):
    if not isinstance(values, list):
        return []
    out = []
    for item in values:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().upper()
        label = str(item.get("label") or key).strip()
        tag_type = str(item.get("type") or "").strip()
        if not key and not label:
            continue
        out.append({
            "key": key,
            "label": label or key,
            "type": tag_type,
        })
    return out


def _normalize_pregame_h1_record(value):
    if not isinstance(value, dict):
        return None
    out = {}
    for field in (
        "home_w", "home_l", "away_w", "away_l",
        "l10_home_w", "l10_home_l", "l10_away_w", "l10_away_l",
    ):
        if field in value:
            out[field] = _to_int_or_none(value.get(field))
    if not out:
        return None
    if all(v is None for v in out.values()):
        return None
    return out


def _normalize_pregame_matchup_meta(value):
    if not isinstance(value, dict):
        return None
    return {
        "team_id": str(value.get("team_id") or "").strip(),
        "abbr": str(value.get("abbr") or "").strip(),
        "name": str(value.get("name") or "").strip(),
        "moneyline": str(value.get("moneyline") or "").strip(),
        "spread": str(value.get("spread") or "").strip(),
        "record": str(value.get("record") or "").strip(),
        "streak": str(value.get("streak") or "").strip(),
    }


def _normalize_pregame_snapshot(value):
    if not isinstance(value, dict):
        return None
    captured_at = _normalize_history_date(value.get("captured_at"))
    if not isinstance(captured_at, str) or not captured_at:
        captured_at = _now_utc_iso()

    return {
        "captured_at": captured_at,
        "spread_detail": str(value.get("spread_detail") or "").strip(),
        "spread_fav_id": str(value.get("spread_fav_id") or "").strip(),
        "home_tags": _normalize_pregame_tags(value.get("home_tags")),
        "away_tags": _normalize_pregame_tags(value.get("away_tags")),
        "home_h1_record": _normalize_pregame_h1_record(value.get("home_h1_record")),
        "away_h1_record": _normalize_pregame_h1_record(value.get("away_h1_record")),
        "home_matchup_meta": _normalize_pregame_matchup_meta(value.get("home_matchup_meta")),
        "away_matchup_meta": _normalize_pregame_matchup_meta(value.get("away_matchup_meta")),
    }


def _default_replay_mode(source):
    if str(source or "").strip().lower() == BACKFILL_SOURCE:
        return "poll_emulated"
    return "live_poll"


def _sort_metric_value(value):
    numeric = _to_float_or_none(value)
    return numeric if numeric is not None else -1.0


def compute_config_fingerprint(config):
    payload = {
        "conditions": (config or {}).get("conditions", []),
        "compound_conditions": (config or {}).get("compound_conditions", []),
        "postgame_outcome_criteria": (config or {}).get("postgame_outcome_criteria", {}),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _normalize_decay_lambda(decay_lambda):
    value = _to_float_or_none(decay_lambda)
    if value is None or value < 0:
        return 0.0
    return value


def _classify_spread_role(team_id, spread_fav_id):
    """Return 'favorite', 'underdog', or '' if spread data unavailable."""
    tid = str(team_id or "").strip()
    fav = str(spread_fav_id or "").strip()
    if not tid or not fav:
        return ""
    return "favorite" if tid == fav else "underdog"


def _parse_team_spread_line(rec, team_id):
    """Return the numeric spread line for team_id from pregame_snapshot, or None."""
    snap = rec.get("pregame_snapshot") or {}
    tid = str(team_id or "").strip()
    if not tid:
        return None
    for side in ("home_matchup_meta", "away_matchup_meta"):
        meta = snap.get(side) or {}
        if str(meta.get("team_id") or "").strip() == tid:
            raw = str(meta.get("spread") or "").strip()
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None
    return None


def _did_team_cover_spread(rec, team_id):
    """Return True/False/None whether the team covered the spread."""
    line = _parse_team_spread_line(rec, team_id)
    if line is None:
        return None
    tid = str(team_id or "").strip()
    home_id = str(rec.get("home_id") or "").strip()
    away_id = str(rec.get("away_id") or "").strip()
    home_score = rec.get("home_score")
    away_score = rec.get("away_score")
    if home_score is None or away_score is None:
        return None
    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except (ValueError, TypeError):
        return None
    # Team margin from their perspective (positive = team won by that much)
    if tid == home_id:
        team_margin = home_score - away_score
    elif tid == away_id:
        team_margin = away_score - home_score
    else:
        return None
    # Covered if team_margin + spread_line > 0; push (== 0) returns None (#412 Phase 4.4)
    result = team_margin + line
    if result == 0:
        return None  # push — exclude from ROI
    return result > 0


def _final_team_margin(rec, team_id):
    """Return final team margin or None."""
    tid = str(team_id or "").strip()
    home_id = str(rec.get("home_id") or "").strip()
    away_id = str(rec.get("away_id") or "").strip()
    home_score = rec.get("home_score")
    away_score = rec.get("away_score")
    if home_score is None or away_score is None:
        return None
    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except (ValueError, TypeError):
        return None
    if tid == home_id:
        return home_score - away_score
    elif tid == away_id:
        return away_score - home_score
    return None


def _did_team_cover_live_line(rec, team_id, pregame_spread, margin_at_fire):
    """Return True/False/None — synthetic live line (pregame_spread - margin)."""
    team_margin = _final_team_margin(rec, team_id)
    if team_margin is None:
        return None
    live_line = pregame_spread - margin_at_fire
    result = team_margin + live_line
    if result == 0:
        return None  # push
    return result > 0


def _did_team_cover_bk_line(rec, team_id, bk_spread):
    """Return True/False/None — bookmaker live spread at fire time (#213)."""
    team_margin = _final_team_margin(rec, team_id)
    if team_margin is None:
        return None
    try:
        result = team_margin + float(bk_spread)
        if result == 0:
            return None  # push
        return result > 0
    except (ValueError, TypeError):
        return None


_SEASON_START_MONTH = {"nba": 10, "wnba": 5}


def _game_season_year(rec, league=None):
    """Return season key from a game record's date.

    NBA: '2024-25' (cross-year). WNBA: '2025' (single-year).
    """
    if league is None:
        league = league_config.LEAGUE
    start_month = _SEASON_START_MONTH.get(league, 10)
    try:
        dt = datetime.fromisoformat(str(rec.get("date") or "").replace("Z", "+00:00"))
        us_date = (dt - timedelta(hours=8)).date()
        y = us_date.year if us_date.month >= start_month else us_date.year - 1
        if league == "wnba":
            return str(y)
        return "{}-{}".format(y, str(y + 1)[-2:])
    except Exception:
        return ""


def _parse_history_datetime(date_value):
    if not isinstance(date_value, str):
        return None

    raw = date_value.strip()
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _recency_weight(rec, decay_lambda, now_utc=None):
    lam = _normalize_decay_lambda(decay_lambda)
    if lam <= 0:
        return 1.0

    dt = _parse_history_datetime(rec.get("date")) if isinstance(rec, dict) else None
    if dt is None:
        return 1.0

    now = now_utc or datetime.now(timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return math.exp(-lam * age_days)


def _weighted_mean(rows, value_key, weight_key="weight"):
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        weight = _to_float_or_none(row.get(weight_key))
        if weight is None or weight <= 0:
            continue
        numerator += weight * (1.0 if row.get(value_key) else 0.0)
        denominator += weight
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100.0, 1)


def _weighted_mean_nullable(rows, value_key, weight_key="weight", eligible_key=None):
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        if eligible_key is not None and not row.get(eligible_key):
            continue
        weight = _to_float_or_none(row.get(weight_key))
        if weight is None or weight <= 0:
            continue
        numerator += weight * (1.0 if row.get(value_key) else 0.0)
        denominator += weight
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100.0, 1)


def _effective_sample_nullable(rows, weight_key="weight", eligible_key=None):
    total = 0.0
    for row in rows:
        if eligible_key is not None and not row.get(eligible_key):
            continue
        weight = _to_float_or_none(row.get(weight_key))
        if weight is None or weight <= 0:
            continue
        total += weight
    return round(total, 1)


def _effective_sample(rows, weight_key="weight"):
    total = 0.0
    for row in rows:
        weight = _to_float_or_none(row.get(weight_key))
        if weight is None or weight <= 0:
            continue
        total += weight
    return round(total, 1)


def _normalize_tag_key(tag_value):
    key = str(tag_value or "").strip().upper()
    return key or None


def _build_tag_hits_map(records):
    """
    Build a map of eligible game_id -> set(tags), where eligibility is defined
    as postgame_tags_matched being a list (including empty list).
    """
    tag_hits_map = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        tags = rec.get("postgame_tags_matched")
        if not isinstance(tags, list):
            continue
        game_id = str(rec.get("game_id") or "")
        if not game_id:
            continue
        normalized_tags = {
            key for key in (_normalize_tag_key(tag) for tag in tags) if key
        }
        tag_hits_map[game_id] = normalized_tags
    return tag_hits_map


def _load_configured_tag_keys():
    """
    Return configured post-game tag keys from config postgame_outcome_criteria.tags.
    Includes keys even if they have no historical hits yet.
    """
    if not os.path.exists(CONFIG_FILE):
        return set()

    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return set()

    criteria = cfg.get("postgame_outcome_criteria")
    if not isinstance(criteria, dict):
        return set()

    tags = criteria.get("tags")
    if not isinstance(tags, list):
        return set()

    out = set()
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        key = _normalize_tag_key(tag.get("key"))
        if key:
            out.add(key)
    return out


def _compute_tag_rates(entries, tag_hits_map, known_tags, base_rates):
    """
    Compute per-tag correlation stats for a condition/compound/combo entry list.

    - pct is weighted (uses row weight)
    - hits and n are raw integer counts
    - lift uses pct against tag base rate
    """
    if not known_tags:
        return {}

    tag_hit_weights = defaultdict(float)
    tag_n_weights = defaultdict(float)
    tag_hits_raw = defaultdict(int)
    tag_n_raw = defaultdict(int)

    for row in entries:
        game_id = str(row.get("game_id") or "")
        if game_id not in tag_hits_map:
            continue

        weight = _to_float_or_none(row.get("weight"))
        if weight is None or weight <= 0:
            continue

        hit_tags = tag_hits_map[game_id]
        for tag in known_tags:
            tag_n_weights[tag] += weight
            tag_n_raw[tag] += 1
            if tag in hit_tags:
                tag_hit_weights[tag] += weight
                tag_hits_raw[tag] += 1

    tag_rates = {}
    for tag in known_tags:
        n_raw = int(tag_n_raw[tag])
        hits_raw = int(tag_hits_raw[tag])
        n_weighted = tag_n_weights[tag]
        pct = round(tag_hit_weights[tag] / n_weighted * 100.0, 1) if n_weighted > 0 else 0.0
        base_rate = _to_float_or_none(base_rates.get(tag)) if isinstance(base_rates, dict) else None
        lift = round(pct / (base_rate * 100.0), 2) if base_rate is not None and base_rate > 0 else None
        tag_rates[tag] = {
            "pct": pct,
            "hits": hits_raw,
            "n": n_raw,
            "lift": lift,
        }

    return tag_rates


def _copy_bayes_prior(outcome_key):
    prior = BAYES_PRIORS[outcome_key]
    return {"alpha": float(prior["alpha"]), "beta": float(prior["beta"])}


class BayesianOutcomeStore:
    OUTCOME_KEYS = ("game_upset", "game_close", "team_wins", "game_comeback")

    def __init__(self, conditions=None, updated=None):
        self.conditions = conditions or {}
        self.updated = updated or datetime.now(timezone.utc).isoformat()

    @classmethod
    def _empty_condition(cls):
        return {key: _copy_bayes_prior(key) for key in cls.OUTCOME_KEYS}

    def _ensure_condition(self, condition_name):
        name = str(condition_name or "").strip()
        if not name:
            return None
        if name not in self.conditions:
            self.conditions[name] = self._empty_condition()
        return self.conditions[name]

    def _update_condition(self, condition_name, outcome_key, success):
        condition = self._ensure_condition(condition_name)
        if condition is None or outcome_key not in self.OUTCOME_KEYS:
            return
        bucket = condition[outcome_key]
        bucket["alpha" if success else "beta"] += 1.0

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid Bayesian payload")

        conditions_payload = payload.get("conditions")
        if not isinstance(conditions_payload, dict):
            raise ValueError("Bayesian payload missing conditions")

        normalized = {}
        for condition_name, stats in conditions_payload.items():
            if not isinstance(stats, dict):
                continue
            name = str(condition_name or "").strip()
            if not name:
                continue
            normalized[name] = cls._empty_condition()
            for outcome_key in cls.OUTCOME_KEYS:
                raw = stats.get(outcome_key)
                if not isinstance(raw, dict):
                    continue
                alpha = _to_float_or_none(raw.get("alpha"))
                beta = _to_float_or_none(raw.get("beta"))
                if alpha is None or beta is None or alpha <= 0 or beta <= 0:
                    continue
                normalized[name][outcome_key] = {"alpha": alpha, "beta": beta}

        return cls(conditions=normalized, updated=payload.get("updated"))

    def to_payload(self):
        return {
            "version": BAYES_STATE_VERSION,
            "updated": self.updated,
            "history_fingerprint": list(_history_fingerprint()),
            "conditions": self.conditions,
        }

    def save(self):
        tmp = BAYES_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_payload(), f, indent=2)
        os.replace(tmp, BAYES_STATE_FILE)

    def update_from_record(self, record):
        if not isinstance(record, dict):
            return

        upset_raw = _normalize_bool_or_none(record.get("was_upset"))
        upset = bool(upset_raw)
        close = bool(record.get("was_close"))
        seen = set()
        for condition in record.get("conditions_fired", []):
            if not isinstance(condition, dict):
                continue
            if condition.get("type") == "compound":
                continue

            name = str(condition.get("name", "")).strip()
            if not name:
                continue
            team_marker = str(condition.get("team_id") or condition.get("team") or "").strip()
            key = (name, team_marker, str(record.get("game_id", "")))
            if key in seen:
                continue
            seen.add(key)

            self._update_condition(name, "team_wins", bool(condition.get("team_won")))
            if upset_raw is not None:
                self._update_condition(name, "game_upset", upset)
            self._update_condition(name, "game_close", close)

        self.updated = datetime.now(timezone.utc).isoformat()

    @classmethod
    def rebuild_from_history(cls, records=None, save=True):
        store = cls()
        for record in (records if records is not None else load_history(migrate=True)):
            store.update_from_record(record)
        if save:
            store.save()
        return store

    @classmethod
    def load_or_rebuild(cls, records=None):
        try:
            with open(BAYES_STATE_FILE) as f:
                payload = json.load(f)
            if payload.get("version") != BAYES_STATE_VERSION:
                raise ValueError("Unsupported Bayesian cache version")
            fingerprint = payload.get("history_fingerprint")
            if tuple(fingerprint or ()) != _history_fingerprint():
                raise ValueError("Stale Bayesian cache")
            return cls.from_payload(payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return cls.rebuild_from_history(records=records, save=True)

    def probability(self, condition_name, outcome_key):
        condition = self.conditions.get(str(condition_name or "").strip())
        if not condition or outcome_key not in self.OUTCOME_KEYS:
            return None
        bucket = condition.get(outcome_key) or {}
        alpha = _to_float_or_none(bucket.get("alpha"))
        beta = _to_float_or_none(bucket.get("beta"))
        if alpha is None or beta is None or alpha + beta <= 0:
            return None
        return round(alpha / (alpha + beta) * 100.0, 1)

    def sample_size(self, condition_name, outcome_key="team_wins"):
        condition = self.conditions.get(str(condition_name or "").strip())
        if not condition or outcome_key not in self.OUTCOME_KEYS:
            return 0
        bucket = condition.get(outcome_key) or {}
        alpha = _to_float_or_none(bucket.get("alpha"))
        beta = _to_float_or_none(bucket.get("beta"))
        if alpha is None or beta is None:
            return 0
        prior = BAYES_PRIORS[outcome_key]
        observed = (alpha + beta) - (prior["alpha"] + prior["beta"])
        return max(0, int(round(observed)))

    def condition_stats(self):
        rows = {}
        for condition_name in sorted(self.conditions):
            sample = self.sample_size(condition_name)
            rows[condition_name] = {
                "sample": sample,
                "team_wins": self.probability(condition_name, "team_wins"),
                "game_upset": self.probability(condition_name, "game_upset"),
                "game_close": self.probability(condition_name, "game_close"),
            }
        return rows

    def combined_probability(self, condition_names, outcome_key):
        names = []
        for name in condition_names or []:
            text = str(name or "").strip()
            if text and text not in names and text in self.conditions:
                names.append(text)
        if not names or outcome_key not in self.OUTCOME_KEYS:
            return None, 0

        if len(names) == 1:
            name = names[0]
            return self.probability(name, outcome_key), self.sample_size(name, outcome_key)

        combined = 1.0
        samples = []
        for name in names:
            pct = self.probability(name, outcome_key)
            if pct is None:
                continue
            combined *= max(0.0, min(1.0, pct / 100.0))
            samples.append(self.sample_size(name, outcome_key))

        if not samples:
            return None, 0
        return round(combined * 100.0, 1), min(samples)


def _history_fingerprint():
    """Return a lightweight fingerprint for game_history.json."""
    try:
        st = os.stat(HISTORY_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (None, None)


def _normalize_history_date(date_value):
    """Normalize history date values to ISO UTC timestamps when possible."""
    if not isinstance(date_value, str):
        return date_value

    raw = date_value.strip()
    if not raw:
        return date_value

    # Legacy format support: YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            return date_value

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return date_value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _infer_condition_team_id(rec, cond):
    """Infer missing condition team_id using game-level team metadata."""
    team = str(cond.get("team", "")).strip().lower()
    if not team:
        return ""

    candidates = [
        (str(rec.get("home_name", "")).strip().lower(), str(rec.get("home_id", "")).strip()),
        (str(rec.get("away_name", "")).strip().lower(), str(rec.get("away_id", "")).strip()),
        (str(rec.get("home_abbr", "")).strip().lower(), str(rec.get("home_id", "")).strip()),
        (str(rec.get("away_abbr", "")).strip().lower(), str(rec.get("away_id", "")).strip()),
    ]
    for name, team_id in candidates:
        if team and team == name and team_id:
            return team_id
    return ""


def _normalize_record_updated_at(record):
    normalized = _normalize_history_date(record.get("record_updated_at"))
    if isinstance(normalized, str) and normalized:
        return normalized
    normalized_date = _normalize_history_date(record.get("date"))
    if isinstance(normalized_date, str) and normalized_date:
        return normalized_date
    return _now_utc_iso()


def normalize_game_record(record, allow_legacy=False):
    if not isinstance(record, dict):
        raise ValueError("History record must be an object")

    normalized = dict(record)
    normalized["schema_version"] = str(
        normalized.get("schema_version") or HISTORY_SCHEMA_VERSION
    ).strip() or HISTORY_SCHEMA_VERSION

    game_id = str(normalized.get("game_id") or "").strip()
    if not game_id:
        raise ValueError("History record missing game_id")
    normalized["game_id"] = game_id

    normalized_date = _normalize_history_date(normalized.get("date"))
    if not isinstance(normalized_date, str) or not normalized_date:
        if allow_legacy:
            normalized_date = _now_utc_iso()
        else:
            raise ValueError("History record missing valid date")
    normalized["date"] = normalized_date
    normalized["record_updated_at"] = _normalize_record_updated_at(normalized)

    source = str(normalized.get("source") or (LIVE_SOURCE if allow_legacy else LIVE_SOURCE)).strip().lower()
    if source not in (LIVE_SOURCE, BACKFILL_SOURCE):
        source = LIVE_SOURCE if allow_legacy else source
    if source not in (LIVE_SOURCE, BACKFILL_SOURCE):
        raise ValueError("History record has invalid source")
    normalized["source"] = source

    replay_mode = str(normalized.get("replay_mode") or _default_replay_mode(source)).strip()
    normalized["replay_mode"] = replay_mode or _default_replay_mode(source)

    config_fingerprint = str(
        normalized.get("config_fingerprint") or (LEGACY_CONFIG_FINGERPRINT if allow_legacy else "")
    ).strip()
    if not config_fingerprint:
        raise ValueError("History record missing config_fingerprint")
    normalized["config_fingerprint"] = config_fingerprint
    normalized["unavailable_inputs"] = _normalize_string_list(normalized.get("unavailable_inputs", []))
    raw_snapshot = normalized.get("pregame_snapshot")
    if isinstance(raw_snapshot, dict):
        normalized["pregame_snapshot"] = _normalize_pregame_snapshot(raw_snapshot)
    else:
        normalized["pregame_snapshot"] = None

    for field in (
        "away_abbr", "home_abbr", "away_name", "home_name",
        "away_id", "home_id", "spread_detail", "spread_fav_id", "winner_id",
    ):
        normalized[field] = str(normalized.get(field) or "").strip()

    for field in ("away_score", "home_score", "margin"):
        value = _to_int_or_none(normalized.get(field))
        if value is None:
            raise ValueError("History record missing numeric {}".format(field))
        normalized[field] = value

    normalized["was_upset"] = _normalize_bool_or_none(normalized.get("was_upset"))
    normalized["was_close"] = bool(normalized.get("was_close"))
    normalized["was_comeback"] = bool(normalized.get("was_comeback"))
    # was_blowout: derive from postgame_tags_matched if not already set
    if normalized.get("was_blowout") is None:
        raw_tags_for_blowout = normalized.get("postgame_tags_matched") or []
        normalized["was_blowout"] = "BLOWOUT" in [str(t).strip().upper() for t in raw_tags_for_blowout]
    else:
        normalized["was_blowout"] = bool(normalized.get("was_blowout"))

    # Optional: persisted post-game tag keys (list of uppercase strings e.g. ["UPSET", "BLOWOUT"])
    raw_tags = normalized.get("postgame_tags_matched")
    if isinstance(raw_tags, list):
        normalized["postgame_tags_matched"] = [
            str(k).strip().upper() for k in raw_tags if str(k).strip()
        ]
    else:
        normalized["postgame_tags_matched"] = []

    # Ensure was_comeback=True is reflected in postgame_tags_matched (sync boolean → tag)
    if normalized.get("was_comeback") and "COMEBACK" not in normalized["postgame_tags_matched"]:
        normalized["postgame_tags_matched"].append("COMEBACK")

    winner_id = normalized.get("winner_id", "")
    conditions = normalized.get("conditions_fired", [])
    if not isinstance(conditions, list):
        raise ValueError("History record conditions_fired must be a list")

    normalized_conditions = []
    for cond in conditions:
        if not isinstance(cond, dict):
            if allow_legacy:
                continue
            raise ValueError("Condition entry must be an object")

        new_cond = dict(cond)
        new_cond["name"] = str(new_cond.get("name") or "").strip()
        new_cond["type"] = str(new_cond.get("type") or "").strip()
        new_cond["team"] = str(new_cond.get("team") or "").strip()
        new_cond["quarter_str"] = str(new_cond.get("quarter_str") or "").strip()
        new_cond["direction"] = str(new_cond.get("direction") or "").strip()
        new_cond["team_id"] = str(new_cond.get("team_id") or "").strip()
        if not new_cond["team_id"]:
            inferred = _infer_condition_team_id(normalized, new_cond)
            if inferred:
                new_cond["team_id"] = inferred

        team_won = _normalize_bool_or_none(new_cond.get("team_won"))
        if team_won is None and winner_id and new_cond["team_id"]:
            team_won = winner_id == new_cond["team_id"]
        new_cond["team_won"] = bool(team_won) if team_won is not None else False
        new_cond["alerted"] = bool(new_cond.get("alerted"))

        if "score_margin_at_fire" in new_cond:
            new_cond["score_margin_at_fire"] = _to_int_or_none(new_cond.get("score_margin_at_fire"))
        if "home_score_at_fire" in new_cond:
            new_cond["home_score_at_fire"] = _to_int_or_none(new_cond.get("home_score_at_fire"))
        if "away_score_at_fire" in new_cond:
            new_cond["away_score_at_fire"] = _to_int_or_none(new_cond.get("away_score_at_fire"))
        if "quarter_elapsed_pct" in new_cond:
            pct = _to_float_or_none(new_cond.get("quarter_elapsed_pct"))
            new_cond["quarter_elapsed_pct"] = round(pct, 3) if pct is not None else None
        if "margin_diff" in new_cond:
            md = _to_float_or_none(new_cond.get("margin_diff"))
            new_cond["margin_diff"] = round(md, 3) if md is not None else None
        if "game_time_seconds" in new_cond:
            new_cond["game_time_seconds"] = _to_int_or_none(new_cond.get("game_time_seconds"))
        if "time_anchor" in new_cond:
            new_cond["time_anchor"] = str(new_cond.get("time_anchor") or "").strip()
        if "home_away_role" in new_cond:
            new_cond["home_away_role"] = str(new_cond.get("home_away_role") or "").strip()

        if not all(new_cond.get(field) for field in ("name", "type", "team", "quarter_str")):
            if allow_legacy:
                continue
            raise ValueError("Condition entry missing required fields")

        normalized_conditions.append(new_cond)

    normalized["conditions_fired"] = normalized_conditions

    # predicted_winner_calls: list of live prediction snapshots for this game.
    # Each entry: { ts, predicted_team, predicted_team_id, pct, consensus, blended, basis, correct,
    #               score_margin_at_fire?, home_score_at_fire?, away_score_at_fire?, game_clock? }
    raw_pw_calls = normalized.get("predicted_winner_calls")
    if isinstance(raw_pw_calls, list):
        winner_id_for_pw = normalized.get("winner_id", "")
        normalized_pw = []
        for call in raw_pw_calls:
            if not isinstance(call, dict):
                continue
            entry = {
                "ts":               str(call.get("ts") or "").strip(),
                "predicted_team":   str(call.get("predicted_team") or "").strip(),
                "predicted_team_id":str(call.get("predicted_team_id") or "").strip(),
                "pct":              _to_float_or_none(call.get("pct")),
                "consensus":        str(call.get("consensus") or "").strip(),
                "blended":          bool(call.get("blended")),
                "basis":            str(call.get("basis") or "").strip(),
                "quarter":          str(call.get("quarter") or "").strip(),
            }
            # Preserve score/margin/clock/conditions context captured at call time (#162, #163)
            # and odds/polarity context (#163, #195.7, #197, #207)
            for _pw_field in ("score_margin_at_fire", "home_score_at_fire",
                              "away_score_at_fire", "game_clock", "active_conditions",
                              "polarity_pos", "polarity_neg", "polarity_net",
                              "avg_edge", "_suppressed", "_suppress_reason",
                              "spread_role", "live_moneyline", "live_spread",
                              "bk_spread", "bk_spread_price", "bk_moneyline", "bk_name",
                              "bk_ml_source", "bk_spread_source", "bk_ts",
                              "pw_version", "synthetic",
                              # Quarter/half odds + outcomes (#374)
                              "bk_q_ml", "bk_q_spread", "bk_q_spread_price", "bk_q_ml_source",
                              "bk_h1_ml", "bk_h1_spread", "bk_h1_spread_price", "bk_h1_ml_source",
                              "q_correct", "q_covered", "h1_correct", "h1_covered",
                              # Synthetic odds (#376)
                              "synth_moneyline", "synth_spread", "synth_h1_ml",
                              # Model version (#412 Phase 4.12)
                              "model_schema_version"):
                if call.get(_pw_field) is not None:
                    entry[_pw_field] = call[_pw_field]
            # Resolve correctness: True/False/None (None = unknown, no winner_id yet)
            if winner_id_for_pw and entry["predicted_team_id"]:
                entry["correct"] = winner_id_for_pw == entry["predicted_team_id"]
            elif call.get("correct") is not None:
                entry["correct"] = bool(call["correct"])
            else:
                entry["correct"] = None
            normalized_pw.append(entry)
        normalized["predicted_winner_calls"] = normalized_pw
    else:
        normalized["predicted_winner_calls"] = []

    return normalized


def migrate_history_records(records):
    """Return (migrated_records, changed) for legacy history rows."""
    changed = False
    migrated = []

    for rec in records:
        if not isinstance(rec, dict):
            migrated.append(rec)
            continue

        try:
            new_rec = normalize_game_record(rec, allow_legacy=True)
        except ValueError:
            migrated.append(rec)
            continue

        if new_rec != rec:
            changed = True
        migrated.append(new_rec)

    return migrated, changed


def migrate_history_file():
    """Apply in-place migration for game_history.json when legacy rows exist."""
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    if not isinstance(data, list):
        return False

    migrated, changed = migrate_history_records(data)
    if changed:
        save_history(migrated)
    return changed


_HISTORY_PARSED_CACHE = {"fingerprint": None, "records": None}
_HISTORY_PARSED_LOCK = threading.Lock()


def _wal_append(record):
    """Append a game record to the write-ahead log (JSONL format). (#189)"""
    try:
        with open(WAL_FILE, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # WAL write failure is non-fatal


def replay_wal(records):
    """Replay WAL entries into a records list, skipping duplicates. (#189)

    Returns the number of records recovered from WAL.
    """
    if not os.path.exists(WAL_FILE):
        return 0
    known_ids = {str(r.get("game_id", "")) for r in records}
    recovered = 0
    try:
        with open(WAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                gid = str(rec.get("game_id", ""))
                if not gid or gid in known_ids:
                    continue
                records.append(rec)
                known_ids.add(gid)
                recovered += 1
    except OSError:
        pass
    return recovered


def list_backups():
    """Return list of available backups, newest first. (#189)

    Each entry: {"name": str, "path": str, "records": int, "size": int, "mtime": str}
    """
    import glob
    backup_dir = os.path.join(os.path.dirname(HISTORY_FILE), "runtime_backups")
    pattern = os.path.join(backup_dir, "*", os.path.basename(HISTORY_FILE))
    results = []
    for bak_path in sorted(glob.glob(pattern), reverse=True):
        name = os.path.basename(os.path.dirname(bak_path))
        try:
            st = os.stat(bak_path)
            # Quick record count — count top-level array elements without full parse
            with open(bak_path) as f:
                data = json.load(f)
            count = len(data) if isinstance(data, list) else 0
            # Count files in backup directory
            try:
                bak_dir = os.path.dirname(bak_path)
                file_count = len([f for f in os.listdir(bak_dir) if os.path.isfile(os.path.join(bak_dir, f))])
            except Exception:
                file_count = 0
            results.append({
                "name": name,
                "path": bak_path,
                "records": count,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "file_count": file_count,
            })
        except (OSError, json.JSONDecodeError):
            continue
    return results


def wal_status():
    """Return WAL file status. (#189)"""
    if not os.path.exists(WAL_FILE):
        return {"exists": False, "entries": 0, "size": 0}
    try:
        st = os.stat(WAL_FILE)
        entries = 0
        oldest_ts = None
        newest_ts = None
        with open(WAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries += 1
                try:
                    rec = json.loads(line)
                    ts = rec.get("record_updated_at") or rec.get("date") or ""
                    if ts:
                        if oldest_ts is None or ts < oldest_ts:
                            oldest_ts = ts
                        if newest_ts is None or ts > newest_ts:
                            newest_ts = ts
                except json.JSONDecodeError:
                    continue
        return {
            "exists": True,
            "entries": entries,
            "size": st.st_size,
            "oldest": oldest_ts,
            "newest": newest_ts,
        }
    except OSError:
        return {"exists": False, "entries": 0, "size": 0}


def restore_from_backup(backup_name, replay_wal_entries=True):
    """Restore game_history.json from a named backup, optionally replaying WAL. (#189)

    Returns {"success": bool, "records": int, "wal_recovered": int, "error": str|None}
    """
    backup_dir = os.path.join(os.path.dirname(HISTORY_FILE), "runtime_backups")
    bak_path = os.path.join(backup_dir, backup_name, os.path.basename(HISTORY_FILE))
    if not os.path.exists(bak_path):
        return {"success": False, "records": 0, "wal_recovered": 0, "error": "Backup not found: " + backup_name}
    try:
        with open(bak_path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {"success": False, "records": 0, "wal_recovered": 0, "error": "Invalid backup format"}
        wal_recovered = 0
        if replay_wal_entries:
            wal_recovered = replay_wal(data)
        save_history(data)
        return {"success": True, "records": len(data), "wal_recovered": wal_recovered, "error": None}
    except Exception as e:
        return {"success": False, "records": 0, "wal_recovered": 0, "error": str(e)}


def truncate_wal(keep_after=None):
    """Truncate WAL entries older than keep_after (ISO timestamp). (#189)

    If keep_after is None, truncates the entire WAL.
    Returns the number of entries removed.
    """
    if not os.path.exists(WAL_FILE):
        return 0
    if keep_after is None:
        try:
            os.unlink(WAL_FILE)
        except OSError:
            pass
        return 0
    kept = []
    removed = 0
    try:
        with open(WAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("record_updated_at") or rec.get("date") or ""
                    if ts >= keep_after:
                        kept.append(line)
                    else:
                        removed += 1
                except json.JSONDecodeError:
                    removed += 1
        # Rewrite WAL with kept entries using atomic pattern
        tmp = WAL_FILE + ".tmp.{}".format(os.getpid())
        with open(tmp, "w") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WAL_FILE)
    except OSError:
        pass
    return removed


def _attempt_recovery_from_backup():
    """Try to restore game_history.json from the latest runtime backup.

    Returns the recovered records list, or None if recovery failed.
    """
    import glob
    backup_dir = os.path.join(os.path.dirname(HISTORY_FILE), "runtime_backups")
    pattern = os.path.join(backup_dir, "*", os.path.basename(HISTORY_FILE))
    backups = sorted(glob.glob(pattern), reverse=True)  # newest first
    for bak_path in backups:
        try:
            with open(bak_path) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                # Preserve corrupted file for investigation
                corrupted_path = HISTORY_FILE + ".corrupted"
                try:
                    os.replace(HISTORY_FILE, corrupted_path)
                except OSError:
                    pass
                # Replay WAL to recover games added since backup (#189)
                wal_recovered = replay_wal(data)
                if wal_recovered:
                    import sys
                    print("[RECOVERY] WAL replay: {} records recovered".format(wal_recovered),
                          file=sys.stderr, flush=True)
                save_history(data)
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def load_history(migrate=False):
    """Return list of game records from game_history.json.

    Caches the parsed result in memory keyed by file fingerprint (mtime+size)
    to avoid re-parsing the 61MB JSON file on every call.
    """
    fp = _history_fingerprint()

    # Fast path: return cached records if fingerprint matches
    with _HISTORY_PARSED_LOCK:
        if not migrate and _HISTORY_PARSED_CACHE["fingerprint"] == fp and _HISTORY_PARSED_CACHE["records"] is not None:
            return _HISTORY_PARSED_CACHE["records"]

    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []

        if migrate:
            migrated, changed = migrate_history_records(data)
            if changed:
                save_history(migrated)
            return migrated

        # Cache the parsed result
        with _HISTORY_PARSED_LOCK:
            _HISTORY_PARSED_CACHE["fingerprint"] = fp
            _HISTORY_PARSED_CACHE["records"] = data

        return data
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        # ── CORRUPTION DETECTED (#188) ──
        import sys
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc).isoformat()
        _msg = "[CRITICAL] game_history.json CORRUPTED at {}: {}".format(_now, exc)
        print(_msg, file=sys.stderr, flush=True)

        # Write marker file for server/dashboard to detect
        try:
            with open(HISTORY_FILE + ".corrupted_at", "w") as _mf:
                _mf.write("{}\n{}\n".format(_now, exc))
        except OSError:
            pass

        # Attempt auto-recovery from latest backup
        _recovered = _attempt_recovery_from_backup()
        if _recovered is not None:
            print("[RECOVERY] Restored game_history.json from backup ({} records)".format(
                len(_recovered)), file=sys.stderr, flush=True)
            with _HISTORY_PARSED_LOCK:
                _HISTORY_PARSED_CACHE["fingerprint"] = _history_fingerprint()
                _HISTORY_PARSED_CACHE["records"] = _recovered
            return _recovered

        # Fallback: serve last-known-good cached data
        with _HISTORY_PARSED_LOCK:
            if _HISTORY_PARSED_CACHE["records"] is not None:
                print("[FALLBACK] Serving {} cached records (stale)".format(
                    len(_HISTORY_PARSED_CACHE["records"])), file=sys.stderr, flush=True)
                return _HISTORY_PARSED_CACHE["records"]
        return []


def save_history(records):
    # Use PID-based temp filename to prevent concurrent process clobber (#188)
    tmp = HISTORY_FILE + ".tmp.{}".format(os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(records, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Validate before replace — re-parse to catch corruption (#188)
        with open(tmp) as check:
            validated = json.load(check)
            if not isinstance(validated, list):
                raise ValueError("save_history: validated data is not a list")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        # Clean up corrupt temp file, re-raise so caller knows save failed
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Invalidate parsed-records cache so next load_history() re-reads from disk
    with _HISTORY_PARSED_LOCK:
        _HISTORY_PARSED_CACHE["fingerprint"] = None
        _HISTORY_PARSED_CACHE["records"] = None
    _clear_outcomes_cache()


def append_game_record(record):
    """
    Append a completed game record to game_history.json.
    record schema:
    {
      "game_id":       str,
      "date":          ISO str,
      "away_abbr":     str,   "home_abbr":     str,
      "away_name":     str,   "home_name":     str,
      "away_score":    int,   "home_score":    int,
      "away_id":       str,   "home_id":       str,
      "spread_detail": str,   # e.g. "BOS -5.5"
      "spread_fav_id": str,   # team_id of favourite
      "was_upset":     bool,
      "was_close":     bool,  # margin <= 5
      "was_comeback":  bool,  # Q4 comeback
      "was_blowout":   bool,  # BLOWOUT post-game tag
      "margin":        int,
      "winner_id":     str,
            "pregame_snapshot": {
                "captured_at": str,
                "spread_detail": str,
                "spread_fav_id": str,
                "home_tags": [
                    {"key": str, "label": str, "type": str}
                ],
                "away_tags": [
                    {"key": str, "label": str, "type": str}
                ],
                "home_h1_record": dict | None,
                "away_h1_record": dict | None,
            } | None,
      "conditions_fired": [
        {
          "name":        str,
          "type":        str,
          "team":        str,
          "team_id":     str,
          "quarter_str": str,
          "direction":   str,
          "alerted":     bool,
          "team_won":    bool,   # did the team that triggered this condition win?
        }
      ]
    }
    """
    record = normalize_game_record(record, allow_legacy=False)

    # Write to WAL first — survives corruption of game_history.json (#189)
    _wal_append(record)

    records = load_history(migrate=True)

    existing_idx = None
    for idx, existing in enumerate(records):
        if str(existing.get("game_id", "")) == str(record.get("game_id", "")):
            existing_idx = idx
            break

    if existing_idx is not None:
        incoming_ts = _parse_history_datetime(record.get("record_updated_at")) or _parse_history_datetime(record.get("date"))
        existing_ts = _parse_history_datetime(records[existing_idx].get("record_updated_at")) or _parse_history_datetime(records[existing_idx].get("date"))
        if existing_ts is not None and incoming_ts is not None and incoming_ts < existing_ts:
            return False
        records[existing_idx] = record
    else:
        records.append(record)

    save_history(records)
    BayesianOutcomeStore.rebuild_from_history(records=records, save=True)
    ConditionEdgeStore.rebuild_from_history(records=records, save=True)
    return True


# ── Outcome computation ───────────────────────────────────────────────────────

def compute_condition_stats(records, min_sample=MIN_SAMPLE, decay_lambda=0.0, known_tags=None, base_rates=None, spread_role="", margin_at_fire=""):
    """
    For each condition, compute outcome rates across all games it fired in.

    spread_role: "" = all, "favorite" = only fires where team was spread fav,
                 "underdog" = only fires where team was spread underdog.
    margin_at_fire: "" = all, "trailing" = score_margin_at_fire <= 0,
                    "leading" = score_margin_at_fire >= 0.

    Returns dict keyed by condition name:
    {
      "condition_name": {
        "games":       int,   # number of times this condition fired (unique game+team)
        "team_wins":   float, # % of times the team that triggered it won
        "game_upset":  float, # % of games it fired in that were upsets
        "game_close":  float, # % of games it fired in that were close (margin <=5)
        "game_comeback": float,
        "any_team_wins": float, # % games where ANY team with this condition won
        "spread_covered_pct": float | None,  # % of fires where team covered spread
      }
    }
    """
    norm_role = (spread_role or "").strip().lower()
    norm_maf = (margin_at_fire or "").strip().lower()  # "trailing" or "leading" (#153)
    # group by (condition_name, team) per game — one entry per unique fire
    cond_map = defaultdict(list)   # cond_name -> list of {team_won, upset, close, comeback}
    now_utc = datetime.now(timezone.utc)
    tag_hits_map = _build_tag_hits_map(records)
    known_tags = sorted(known_tags or [])

    for rec in records:
        upset_raw = _normalize_bool_or_none(rec.get("was_upset"))
        close    = bool(rec.get("was_close"))
        comeback = bool(rec.get("was_comeback"))
        blowout  = bool(rec.get("was_blowout"))
        weight   = _recency_weight(rec, decay_lambda, now_utc)
        spread_fav_id = str(rec.get("spread_fav_id") or "").strip()

        seen = set()
        for cf in rec.get("conditions_fired", []):
            # Exclude compound conditions — they have their own section
            if cf.get("type") == "compound":
                continue
            name     = cf.get("name", "")
            team_won = bool(cf.get("team_won"))
            team_id  = str(cf.get("team_id") or "").strip()
            key      = (name, cf.get("team",""), rec.get("game_id",""))
            if key in seen:
                continue
            # Apply spread_role filter
            if norm_role:
                role = _classify_spread_role(team_id, spread_fav_id)
                if not role:
                    continue  # no spread data — skip when filtering
                if role != norm_role:
                    continue
            # Apply margin_at_fire filter (#153)
            if norm_maf:
                maf_val = cf.get("score_margin_at_fire")
                if maf_val is None:
                    continue  # no margin data — skip when filtering
                if norm_maf == "trailing" and maf_val > 0:
                    continue
                if norm_maf == "leading" and maf_val < 0:
                    continue
            seen.add(key)
            margin_diff = cf.get("margin_diff")
            covered = _did_team_cover_spread(rec, team_id)
            cond_map[name].append({
                "game_id": str(rec.get("game_id") or ""),
                "team_won": team_won,
                "upset":    bool(upset_raw),
                "upset_known": upset_raw is not None,
                "close":    close,
                "comeback": comeback,
                "blowout":  blowout,
                "weight":   weight,
                "margin_diff": margin_diff if isinstance(margin_diff, (int, float)) else None,
                "spread_covered": covered,
            })

    out = {}
    for name, entries in cond_map.items():
        n = len(entries)
        effective_n = _effective_sample(entries)
        if n < min_sample or effective_n < float(min_sample):
            continue
        diffs = [e["margin_diff"] for e in entries if e.get("margin_diff") is not None]
        pos_diff_events = sum(1 for d in diffs if d > 0)
        neg_diff_events = sum(1 for d in diffs if d < 0)
        covered_known = [e for e in entries if e.get("spread_covered") is not None]
        covered_yes = sum(1 for e in covered_known if e["spread_covered"])
        unique_games = len(set(e.get("game_id", "") for e in entries if e.get("game_id")))
        out[name] = {
            "games":        n,
            "unique_games": unique_games,
            "effective_n":  effective_n,
            "team_wins":    _weighted_mean(entries, "team_won"),
            "game_upset":   _weighted_mean_nullable(entries, "upset", eligible_key="upset_known"),
            "game_upset_effective_n": _effective_sample_nullable(entries, eligible_key="upset_known"),
            "game_close":   _weighted_mean(entries, "close"),
            "game_comeback":_weighted_mean(entries, "comeback"),
            "game_blowout": _weighted_mean(entries, "blowout"),
            "tag_rates": _compute_tag_rates(entries, tag_hits_map, known_tags, base_rates or {}),
            "margin_diff_events": len(diffs),
            "positive_margin_diff_pct": round(pos_diff_events / len(diffs) * 100, 1) if diffs else None,
            "negative_margin_diff_pct": round(neg_diff_events / len(diffs) * 100, 1) if diffs else None,
            "avg_margin_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "spread_covered_pct": round(covered_yes / len(covered_known) * 100, 1) if covered_known else None,
            "spread_covered_n": len(covered_known),
            "polarity": classify_polarity(name),
        }
    return out


def compute_compound_stats(records, min_sample=MIN_SAMPLE, decay_lambda=0.0, known_tags=None, base_rates=None, spread_role="", margin_at_fire=""):
    """
    For each compound condition (type == "compound"), compute outcome rates
    across all games it fired in — separate from regular conditions.

    Returns dict keyed by compound condition name (same schema as compute_condition_stats).
    """
    norm_role = (spread_role or "").strip().lower()
    norm_maf = (margin_at_fire or "").strip().lower()
    cond_map = defaultdict(list)
    now_utc = datetime.now(timezone.utc)
    tag_hits_map = _build_tag_hits_map(records)
    known_tags = sorted(known_tags or [])

    for rec in records:
        upset_raw = _normalize_bool_or_none(rec.get("was_upset"))
        close    = bool(rec.get("was_close"))
        comeback = bool(rec.get("was_comeback"))
        blowout  = bool(rec.get("was_blowout"))
        weight   = _recency_weight(rec, decay_lambda, now_utc)
        spread_fav_id = str(rec.get("spread_fav_id") or "").strip()

        seen = set()
        for cf in rec.get("conditions_fired", []):
            if cf.get("type") != "compound":
                continue
            name     = cf.get("name", "")
            team_won = bool(cf.get("team_won"))
            team_id  = str(cf.get("team_id") or "").strip()
            key      = (name, cf.get("team", ""), rec.get("game_id", ""))
            if key in seen:
                continue
            if norm_role:
                role = _classify_spread_role(team_id, spread_fav_id)
                if not role or role != norm_role:
                    continue
            if norm_maf:
                maf_val = cf.get("score_margin_at_fire")
                if maf_val is None:
                    continue
                if norm_maf == "trailing" and maf_val > 0:
                    continue
                if norm_maf == "leading" and maf_val < 0:
                    continue
            seen.add(key)
            margin_diff = cf.get("margin_diff")
            covered = _did_team_cover_spread(rec, team_id)
            cond_map[name].append({
                "game_id": str(rec.get("game_id") or ""),
                "team_won": team_won,
                "upset":    bool(upset_raw),
                "upset_known": upset_raw is not None,
                "close":    close,
                "comeback": comeback,
                "blowout":  blowout,
                "weight":   weight,
                "margin_diff": margin_diff if isinstance(margin_diff, (int, float)) else None,
                "spread_covered": covered,
            })

    out = {}
    for name, entries in cond_map.items():
        n = len(entries)
        effective_n = _effective_sample(entries)
        if n < min_sample or effective_n < float(min_sample):
            continue
        diffs = [e["margin_diff"] for e in entries if e.get("margin_diff") is not None]
        pos_diff_events = sum(1 for d in diffs if d > 0)
        neg_diff_events = sum(1 for d in diffs if d < 0)
        covered_known = [e for e in entries if e.get("spread_covered") is not None]
        covered_yes = sum(1 for e in covered_known if e["spread_covered"])
        unique_games_c = len(set(e.get("game_id", "") for e in entries if e.get("game_id")))
        out[name] = {
            "games":         n,
            "unique_games":  unique_games_c,
            "effective_n":   effective_n,
            "team_wins":     _weighted_mean(entries, "team_won"),
            "game_upset":    _weighted_mean_nullable(entries, "upset", eligible_key="upset_known"),
            "game_upset_effective_n": _effective_sample_nullable(entries, eligible_key="upset_known"),
            "game_close":    _weighted_mean(entries, "close"),
            "game_comeback": _weighted_mean(entries, "comeback"),
            "game_blowout":  _weighted_mean(entries, "blowout"),
            "tag_rates": _compute_tag_rates(entries, tag_hits_map, known_tags, base_rates or {}),
            "margin_diff_events": len(diffs),
            "positive_margin_diff_pct": round(pos_diff_events / len(diffs) * 100, 1) if diffs else None,
            "negative_margin_diff_pct": round(neg_diff_events / len(diffs) * 100, 1) if diffs else None,
            "avg_margin_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "spread_covered_pct": round(covered_yes / len(covered_known) * 100, 1) if covered_known else None,
            "spread_covered_n": len(covered_known),
        }
    return out


_MAX_CONDITIONS_PER_TEAM_COMBO = 12  # Cap per-team conditions entering combo generation to limit memory


def compute_combination_stats(records, max_combo=3, min_sample=MIN_SAMPLE, decay_lambda=0.0, known_tags=None, base_rates=None, spread_role="", margin_at_fire=""):
    """
    For condition combos (pairs and triples) that co-fired in the same game,
    compute outcome rates. Returns top combos sorted by upset% desc.

    Returns list of:
    {
      "conditions": [str, ...],
      "games":      int,
      "team_wins":  float,
      "game_upset": float,
      "game_close": float,
      "game_comeback": float,
    }
    """
    norm_role = (spread_role or "").strip().lower()
    norm_maf = (margin_at_fire or "").strip().lower()
    # Pre-pass: count global condition frequency so we can cap per-team inputs
    # to the most frequently-fired conditions, keeping combo stats meaningful.
    global_cond_freq = defaultdict(int)
    for rec in records:
        seen = set()
        for cf in rec.get("conditions_fired", []) or []:
            if not isinstance(cf, dict):
                continue
            name = str(cf.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                global_cond_freq[name] += 1

    # Pre-filter: conditions that appear fewer than min_sample times globally
    # can never form a qualifying combo, so exclude them from combo generation.
    _eligible_conditions = frozenset(n for n, c in global_cond_freq.items() if c >= min_sample)

    combo_map = defaultdict(list)
    now_utc = datetime.now(timezone.utc)
    tag_hits_map = _build_tag_hits_map(records)
    known_tags = sorted(known_tags or [])

    for rec in records:
        upset_raw = _normalize_bool_or_none(rec.get("was_upset"))
        close    = bool(rec.get("was_close"))
        comeback = bool(rec.get("was_comeback"))
        blowout  = bool(rec.get("was_blowout"))
        weight   = _recency_weight(rec, decay_lambda, now_utc)
        spread_fav_id = str(rec.get("spread_fav_id") or "").strip()

        # Build combinations only within same-team condition contexts.
        by_team = defaultdict(set)
        team_won_map = {}
        # Collect margin_diffs per team from all conditions that fired for that team
        team_margin_diffs_map = defaultdict(list)
        for cf in rec.get("conditions_fired", []) or []:
            if not isinstance(cf, dict):
                continue
            name = str(cf.get("name") or "").strip()
            tid = str(cf.get("team_id") or "").strip()
            if not name or not tid or name not in _eligible_conditions:
                continue
            # Apply spread_role filter at condition-fire level
            if norm_role:
                role = _classify_spread_role(tid, spread_fav_id)
                if not role or role != norm_role:
                    continue
            # Apply margin_at_fire filter (#153)
            if norm_maf:
                maf_val = cf.get("score_margin_at_fire")
                if maf_val is None:
                    continue
                if norm_maf == "trailing" and maf_val > 0:
                    continue
                if norm_maf == "leading" and maf_val < 0:
                    continue
            by_team[tid].add(name)
            if tid not in team_won_map:
                team_won_map[tid] = bool(cf.get("team_won"))
            margin_diff = cf.get("margin_diff")
            if isinstance(margin_diff, (int, float)):
                team_margin_diffs_map[tid].append(margin_diff)

        for tid, names in by_team.items():
            # Cap to the most globally frequent conditions to bound memory.
            if len(names) > _MAX_CONDITIONS_PER_TEAM_COMBO:
                names = set(sorted(names, key=lambda n: global_cond_freq.get(n, 0), reverse=True)[:_MAX_CONDITIONS_PER_TEAM_COMBO])
            fired_names = sorted(names)
            if len(fired_names) < 2:
                continue
            team_won = bool(team_won_map.get(tid, False))
            team_diffs = team_margin_diffs_map.get(tid, [])
            avg_team_diff = sum(team_diffs) / len(team_diffs) if team_diffs else None
            covered = _did_team_cover_spread(rec, tid)
            for size in range(2, min(max_combo + 1, len(fired_names) + 1)):
                # Pack as tuple: (game_id, team_id, team_won, upset, upset_known, close, comeback, blowout, weight, margin_diff, spread_covered)
                entry = (str(rec.get("game_id") or ""), tid, team_won, bool(upset_raw), upset_raw is not None, close, comeback, blowout, weight, avg_team_diff, covered)
                for combo in combinations(fired_names, size):
                    combo_map[combo].append(entry)

    # Tuple indices: 0=game_id, 1=team_id, 2=team_won, 3=upset, 4=upset_known,
    #                5=close, 6=comeback, 7=blowout, 8=weight, 9=margin_diff, 10=spread_covered
    results = []
    for combo, entries in combo_map.items():
        game_map = {}  # game_id -> merged per-game entry
        for e in entries:
            gid = e[0]
            if gid not in game_map:
                game_map[gid] = {
                    "game_id": gid,
                    "team_won": e[2],
                    "upset": e[3],
                    "upset_known": e[4],
                    "close": e[5],
                    "comeback": e[6],
                    "blowout": e[7],
                    "weight": e[8],
                    "_margin_diffs": [e[9]] if e[9] is not None else [],
                    "_covered_vals": [e[10]] if e[10] is not None else [],
                }
            else:
                if e[2]:
                    game_map[gid]["team_won"] = True
                if e[9] is not None:
                    game_map[gid]["_margin_diffs"].append(e[9])
                if e[10] is not None:
                    game_map[gid]["_covered_vals"].append(e[10])

        per_game_entries = []
        for gid, gdata in game_map.items():
            avg_diff = (
                sum(gdata["_margin_diffs"]) / len(gdata["_margin_diffs"])
                if gdata["_margin_diffs"] else None
            )
            cv = gdata["_covered_vals"]
            covered = any(cv) if cv else None
            per_game_entries.append({
                "game_id": gid,
                "team_won": gdata["team_won"],
                "upset": gdata["upset"],
                "upset_known": gdata["upset_known"],
                "close": gdata["close"],
                "comeback": gdata["comeback"],
                "blowout": gdata["blowout"],
                "weight": gdata["weight"],
                "margin_diff": avg_diff,
                "spread_covered": covered,
            })

        fires = len(entries)  # total team-level entries before game dedup
        n = len(per_game_entries)
        effective_n = _effective_sample(per_game_entries)
        if n < min_sample or effective_n < float(min_sample):
            continue
        diffs = [e["margin_diff"] for e in per_game_entries if e.get("margin_diff") is not None]
        pos_diff_events = sum(1 for d in diffs if d > 0)
        neg_diff_events = sum(1 for d in diffs if d < 0)
        covered_known = [e for e in per_game_entries if e.get("spread_covered") is not None]
        covered_yes = sum(1 for e in covered_known if e["spread_covered"])
        results.append({
            "conditions":   list(combo),
            "games":        fires,
            "unique_games": n,
            "effective_n":  effective_n,
            "team_wins":    _weighted_mean(per_game_entries, "team_won"),
            "game_upset":   _weighted_mean_nullable(per_game_entries, "upset", eligible_key="upset_known"),
            "game_upset_effective_n": _effective_sample_nullable(per_game_entries, eligible_key="upset_known"),
            "game_close":   _weighted_mean(per_game_entries, "close"),
            "game_comeback":_weighted_mean(per_game_entries, "comeback"),
            "game_blowout": _weighted_mean(per_game_entries, "blowout"),
            "tag_rates": _compute_tag_rates(per_game_entries, tag_hits_map, known_tags, base_rates or {}),
            "margin_diff_events": len(diffs),
            "positive_margin_diff_pct": round(pos_diff_events / len(diffs) * 100, 1) if diffs else None,
            "negative_margin_diff_pct": round(neg_diff_events / len(diffs) * 100, 1) if diffs else None,
            "avg_margin_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "spread_covered_pct": round(covered_yes / len(covered_known) * 100, 1) if covered_known else None,
            "spread_covered_n": len(covered_known),
        })

    # Sort by games desc first (so client-side re-sort by any column works correctly),
    # then upset% desc as secondary — client receives all qualifying combos (capped at 500).
    results.sort(key=lambda x: (-x["games"], -_sort_metric_value(x.get("game_upset")), -_sort_metric_value(x.get("game_close"))))
    return results[:500]


def compute_tag_outcomes(records, decay_lambda=0.0):
    """
    For each post-game tag key (e.g. "UPSET", "BLOWOUT") compute how many games
    had that tag fire, as a fraction of all tracked games.

    Only includes tags present on records that have the `postgame_tags_matched` field
    (i.e. games recorded after Phase 1 rollout).  Legacy records with no field are
    excluded from the denominator for each tag, so rates are not diluted by missing data.

    Returns:
    {
      "UPSET":   {"games": 15, "eligible_games": 68, "rate": 0.221},
      "BLOWOUT": {"games":  8, "eligible_games": 68, "rate": 0.118},
      ...
    }
    where eligible_games = number of records that have the postgame_tags_matched field.
    """
    norm_decay = _normalize_decay_lambda(decay_lambda)
    now_utc = datetime.now(timezone.utc)

    eligible_count = 0
    eligible_weight = 0.0
    tag_hits = defaultdict(lambda: {"count": 0, "weight": 0.0})

    for rec in records:
        if not isinstance(rec, dict):
            continue
        tags = rec.get("postgame_tags_matched")
        if not isinstance(tags, list):
            continue  # legacy record without field — skip entirely

        weight = _recency_weight(rec, norm_decay, now_utc)
        eligible_count += 1
        eligible_weight += weight

        for tag_key in tags:
            key = str(tag_key or "").strip().upper()
            if not key:
                continue
            tag_hits[key]["count"] += 1
            tag_hits[key]["weight"] += weight

    result = {}
    for tag_key in sorted(tag_hits):
        hit = tag_hits[tag_key]
        if eligible_weight > 0:
            rate = round(hit["weight"] / eligible_weight, 4)
        else:
            rate = None
        result[tag_key] = {
            "games": hit["count"],
            "eligible_games": eligible_count,
            "rate": rate,
        }
    return result


def _edge_range_key(avg_edge):
    """Classify avg_edge into a bucket key matching ROI Matrix dimensions (#352)."""
    if avg_edge is None:
        return ""
    try:
        e = float(avg_edge)
    except (TypeError, ValueError):
        return ""
    if e >= 10:
        return "10+"
    if e >= 5:
        return "5-10"
    if e >= 0:
        return "0-5"
    return "<0"


def _margin_key(score_margin_at_fire):
    """Classify score_margin_at_fire into leading/trailing matching ROI Matrix (#352)."""
    if score_margin_at_fire is None:
        return ""
    try:
        return "leading" if float(score_margin_at_fire) >= 0 else "trailing"
    except (TypeError, ValueError):
        return ""


def scenario_key_from_call(call, spread_fav_id=""):
    """
    Build a stable scenario key for a PW call from 4 dimensions (#352):
    spread_role, margin, edge_range, quarter.

    Returns a pipe-delimited string like "favorite|leading|5-10|Q3",
    or "" if any required dimension is missing.

    Bucket definitions match the ROI Matrix exactly.

    If the call already has a 'spread_role' field set, uses that directly
    instead of computing from spread_fav_id.
    """
    role = str(call.get("spread_role") or "").strip().lower()
    if not role:
        pred_team_id = str(call.get("predicted_team_id") or "").strip()
        role = _classify_spread_role(pred_team_id, spread_fav_id)
    margin = _margin_key(call.get("score_margin_at_fire"))
    edge = _edge_range_key(call.get("avg_edge"))
    quarter = str(call.get("quarter") or "").strip()
    if not role or not margin or not edge or not quarter:
        return ""
    return f"{role}|{margin}|{edge}|{quarter}"


def _filter_calls_by_selection(calls, call_selection):
    """Apply call_selection filter: first, first_per_quarter, last, or all."""
    sel = (call_selection or "").strip().lower()
    if not sel or sel == "all" or not calls:
        return calls
    if sel == "first":
        return [min(calls, key=lambda c: c.get("ts") or "")]
    if sel == "last":
        return [max(calls, key=lambda c: c.get("ts") or "")]
    if sel == "first_per_quarter":
        by_q = {}
        for c in calls:
            q = c.get("quarter", "")
            if q not in by_q or (c.get("ts") or "") < (by_q[q].get("ts") or ""):
                by_q[q] = c
        return sorted(by_q.values(), key=lambda c: c.get("ts") or "")
    return calls


def compute_predicted_winner_accuracy(records, spread_role="", margin_at_fire="",
                                      quarter="", matrix=True, matrix_pins=None,
                                      call_selection="", custom_pivot=None):
    """
    Aggregate predicted_winner_calls across all history records to produce
    accuracy metrics for the Analysis tab.

    spread_role: "" = all, "favorite" = only calls predicting the spread fav,
                 "underdog" = only calls predicting the spread underdog.
    margin_at_fire: "" = all, "trailing" = score_margin_at_fire <= 0,
                    "leading" = score_margin_at_fire >= 0 (#158).
    matrix: if True, compute by_matrix cross-dimensional pivots.
    matrix_pins: dict of pinned dimension values, e.g.
                 {"quarter": "Q3", "spread_role": "underdog"}.
                 Calls not matching pinned values are excluded from matrix buckets.

    Returns:
    {
      "total_calls":   int,   # calls with a known correct/incorrect outcome
      "correct":       int,
      "incorrect":     int,
      "accuracy_pct":  float | None,   # correct / total_calls * 100
      "total_games":   int,   # games that had at least one resolved call
      "games_correct": int,   # games where >=1 call was correct
      "pending_calls": int,   # calls where correct=None (outcome unknown)
      # Breakdown by confidence band
      "by_threshold": {
        "50": {"calls": int, "correct": int, "pct": float|None},
        "60": ...,
        "70": ...,
        "80": ...,
        "90": ...,
      },
      # Breakdown by consensus type
      "by_consensus": {
        "strong":           {"calls": int, "correct": int, "pct": float|None},
        "conflicted":       ...,
        "historical_only":  ...,
        "ml_only":          ...,
        "":                 ...,   # no consensus badge
      },
      # Breakdown by quarter when call was made
      "by_quarter": {
        "Q1": {"calls": int, "correct": int, "pct": float|None},
        "Q2": ..., ...
      },
      # Breakdown by spread role of predicted team
      "by_spread_role": {
        "favorite": {"calls": int, "correct": int, "pct": float|None, ...},
        "underdog": ...,
      },
    }
    """
    norm_role = (spread_role or "").strip().lower()
    norm_maf_pw = (margin_at_fire or "").strip().lower()  # #158
    norm_quarter = (quarter or "").strip().upper()
    total_calls   = 0
    correct       = 0
    incorrect     = 0
    pending_calls = 0
    suppressed_calls = 0
    games_with_calls = 0
    games_correct    = 0
    flat_pnl = 0.0
    ml_pnl   = 0.0
    ml_coverage = 0   # calls where actual moneyline was available
    live_line_pnl = 0.0
    live_line_count = 0  # calls where synthetic live line could be computed
    bk_line_pnl = 0.0
    bk_line_count = 0  # calls where bookmaker live line available
    bk_odds_pnl = 0.0
    bk_odds_count = 0  # calls where bookmaker moneyline available at fire time
    _top_spread_cover_yes = set()  # game IDs where predicted team covered
    _top_spread_cover_no  = set()  # game IDs where predicted team did not cover
    _top_covered_not_won  = set()  # game IDs: covered spread but didn't win outright (#153)
    _top_won_not_covered  = set()  # game IDs: won outright but didn't cover spread (#153)

    # Collect all known tag keys for columns (#148)
    all_tag_keys = sorted(_load_configured_tag_keys() | {"UPSET", "CLOSE", "COMEBACK", "BLOWOUT"})
    # Pre-build game->tags and game->spread_cover maps for fast lookup
    _tag_map = _build_tag_hits_map(records)

    def _new_bucket():
        return {"calls": 0, "correct": 0, "_game_ids": set(), "_game_ids_correct": set(),
                "flat_pnl": 0.0, "ml_pnl": 0.0, "ml_coverage": 0,
                "live_line_pnl": 0.0, "live_line_count": 0,
                "bk_line_pnl": 0.0, "bk_line_count": 0,
                "_spread_cover_yes": set(), "_spread_cover_no": set(),
                "_covered_not_won": set(), "_won_not_covered": set(),
                "bk_odds_pnl": 0.0, "bk_odds_count": 0,
                "_tag_game_ids": {t: set() for t in all_tag_keys}}

    by_threshold = {}
    for band in (50, 60, 70, 80, 90):
        by_threshold[str(band)] = _new_bucket()

    by_consensus = {}
    by_quarter   = {}
    by_spread_role = {}  # "favorite" / "underdog" breakdowns

    by_half = {}
    by_edge_range = {}
    by_margin = {}

    # Matrix cross-dimensional pivots (#150, #349)
    _MATRIX_DIMS = ("threshold", "consensus", "quarter", "spread_role", "half", "edge_range", "margin")
    _MATRIX_PAIRS = [
        ("threshold", "consensus"),
        ("threshold", "quarter"),
        ("threshold", "spread_role"),
        ("threshold", "edge_range"),
        ("threshold", "margin"),
        ("consensus", "quarter"),
        ("consensus", "spread_role"),
        ("consensus", "edge_range"),
        ("consensus", "margin"),
        ("quarter", "spread_role"),
        ("quarter", "edge_range"),
        ("quarter", "margin"),
        ("spread_role", "edge_range"),
        ("spread_role", "margin"),
        ("half", "spread_role"),
        ("half", "edge_range"),
        ("half", "margin"),
        ("half", "quarter"),
        ("half", "consensus"),
        ("half", "threshold"),
        ("edge_range", "margin"),
    ]
    by_matrix = {f"{a}_x_{b}": {} for a, b in _MATRIX_PAIRS} if matrix else None
    by_custom = {}
    _custom = custom_pivot or {}
    _cust_row_dims = _custom.get("row_dims") or []
    _cust_col_dims = _custom.get("col_dims") or []
    _has_custom = bool(_cust_row_dims and _cust_col_dims)
    pins = matrix_pins or {}

    def _lookup_team_ml(rec, team_id):
        """Return parsed moneyline int for the given team_id, or None."""
        snap = rec.get("pregame_snapshot") or {}
        tid = str(team_id or "")
        if not tid:
            return None
        home_meta = snap.get("home_matchup_meta") or {}
        away_meta = snap.get("away_matchup_meta") or {}
        if tid == str(home_meta.get("team_id") or ""):
            return _parse_moneyline(home_meta.get("moneyline"))
        if tid == str(away_meta.get("team_id") or ""):
            return _parse_moneyline(away_meta.get("moneyline"))
        return None

    norm_call_sel = (call_selection or "").strip().lower()

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list) or not calls:
            continue

        game_correct = False
        game_has_resolved_call = False
        game_id = str(rec.get("game_id") or "")
        spread_fav_id = str(rec.get("spread_fav_id") or "").strip()

        # Pre-filter calls by role/margin/quarter, THEN apply call_selection (#183)
        # Exclude suppressed calls from accuracy stats (#231)
        _pre_filtered = []
        for _pf_call in calls:
            if not isinstance(_pf_call, dict):
                continue
            if _pf_call.get("_suppressed"):
                suppressed_calls += 1
                continue
            _pf_pid = str(_pf_call.get("predicted_team_id") or "").strip()
            if norm_role:
                _pf_role = _classify_spread_role(_pf_pid, spread_fav_id)
                if not _pf_role or _pf_role != norm_role:
                    continue
            if norm_maf_pw:
                _pf_m = _pf_call.get("score_margin_at_fire")
                if _pf_m is None:
                    continue
                if norm_maf_pw == "trailing" and _pf_m > 0:
                    continue
                if norm_maf_pw == "leading" and _pf_m < 0:
                    continue
            if norm_quarter:
                _pf_q = str(_pf_call.get("quarter") or "").strip().upper()
                if norm_quarter == "OT":
                    if not _pf_q.startswith("OT"):
                        continue
                elif _pf_q != norm_quarter:
                    continue
            _pre_filtered.append(_pf_call)
        # Apply call_selection after other filters
        calls = _filter_calls_by_selection(_pre_filtered, norm_call_sel)
        if not calls:
            continue

        # Game-level tag and spread data for bucket tracking (#148)
        game_tags = _tag_map.get(game_id, set())

        for call in calls:
            pred_team_id = str(call.get("predicted_team_id") or "").strip()
            call_role = _classify_spread_role(pred_team_id, spread_fav_id)

            # Role/margin/quarter filters already applied in pre-filter above

            c = call.get("correct")
            pct = _to_float_or_none(call.get("pct")) or 0.0
            consensus = str(call.get("consensus") or "").strip().lower()
            quarter   = str(call.get("quarter") or "").strip()

            if c is None:
                pending_calls += 1
                continue

            game_has_resolved_call = True
            total_calls += 1
            is_correct = bool(c)
            if is_correct:
                correct += 1
                game_correct = True
            else:
                incorrect += 1

            # ROI: compute flat and moneyline P&L for this call
            flat_delta = FLAT_WIN_PROFIT if is_correct else -STAKE
            flat_pnl += flat_delta

            team_ml = _lookup_team_ml(rec, pred_team_id)
            has_real_ml = team_ml is not None
            if has_real_ml:
                ml_coverage += 1
            ml_delta = (_ml_win_profit(team_ml) if is_correct else -STAKE)
            ml_pnl += ml_delta

            # Spread cover for this call's predicted team (#148)
            call_spread_cover = _did_team_cover_spread(rec, pred_team_id)
            if game_id and call_spread_cover is True:
                _top_spread_cover_yes.add(game_id)
            elif game_id and call_spread_cover is False:
                _top_spread_cover_no.add(game_id)
            # Covered-not-won / won-not-covered (#153)
            if game_id and call_spread_cover is True and not is_correct:
                _top_covered_not_won.add(game_id)
            elif game_id and call_spread_cover is False and is_correct:
                _top_won_not_covered.add(game_id)

            # Synthetic live line ROI: pregame_spread - margin (#183)
            _call_ll_cover = None
            _call_margin = call.get("score_margin_at_fire")
            _call_spread = None
            if pred_team_id:
                _call_spread = _parse_team_spread_line(rec, pred_team_id)
            if _call_margin is not None and _call_spread is not None:
                _call_ll_cover = _did_team_cover_live_line(
                    rec, pred_team_id, _call_spread, _call_margin)
                if _call_ll_cover is not None:
                    live_line_count += 1
                    if _call_ll_cover:
                        live_line_pnl += FLAT_WIN_PROFIT
                    else:
                        live_line_pnl -= STAKE
            # Bookmaker live line ROI (#213, #356: require bk_spread_price, no fallback #412)
            _call_bk_cover = None
            _call_bk_spread = call.get("bk_spread")
            _call_bk_spr_price = call.get("bk_spread_price")
            if _call_bk_spread is not None and _call_bk_spr_price is not None and pred_team_id:
                try:
                    _call_bk_spr_profit = _ml_win_profit(int(_call_bk_spr_price))
                except (ValueError, TypeError):
                    _call_bk_spr_profit = None
                if _call_bk_spr_profit is not None:
                    _call_bk_cover = _did_team_cover_bk_line(
                        rec, pred_team_id, _call_bk_spread)
                    if _call_bk_cover is not None:
                        bk_line_count += 1
                        if _call_bk_cover:
                            bk_line_pnl += _call_bk_spr_profit
                        else:
                            bk_line_pnl -= STAKE
            # Bookmaker odds (moneyline at PW fire time) ROI (#238, #299)
            _call_bk_ml = call.get("bk_moneyline")
            _bk_ml_valid = False
            _bk_ml_profit = 0.0
            if _call_bk_ml is not None:
                try:
                    _bk_ml_int = int(str(_call_bk_ml).replace("+", ""))
                    if _bk_ml_int != 0:
                        _bk_ml_valid = True
                        _bk_ml_profit = _ml_win_profit(_bk_ml_int) if is_correct else -STAKE
                        bk_odds_count += 1
                        bk_odds_pnl += _bk_ml_profit
                except (ValueError, TypeError):
                    pass

            def _accum_bucket(bucket):
                bucket["calls"] += 1
                if is_correct:
                    bucket["correct"] += 1
                bucket["flat_pnl"] += flat_delta
                bucket["ml_pnl"] += ml_delta
                if has_real_ml:
                    bucket["ml_coverage"] += 1
                if _call_ll_cover is not None:
                    bucket["live_line_count"] += 1
                    bucket["live_line_pnl"] += FLAT_WIN_PROFIT if _call_ll_cover else -STAKE
                if _call_bk_cover is not None:
                    bucket["bk_line_count"] += 1
                    bucket["bk_line_pnl"] += _call_bk_spr_profit if _call_bk_cover else -STAKE
                # BK odds ROI (#299)
                if _bk_ml_valid:
                    bucket["bk_odds_count"] += 1
                    bucket["bk_odds_pnl"] += _bk_ml_profit
                if game_id:
                    bucket["_game_ids"].add(game_id)
                    if is_correct:
                        bucket["_game_ids_correct"].add(game_id)
                    # Track spread cover per game (#148)
                    if call_spread_cover is True:
                        bucket["_spread_cover_yes"].add(game_id)
                    elif call_spread_cover is False:
                        bucket["_spread_cover_no"].add(game_id)
                    # Track covered-but-didn't-win / won-but-didn't-cover (#153)
                    if call_spread_cover is True and not is_correct:
                        bucket["_covered_not_won"].add(game_id)
                    elif call_spread_cover is False and is_correct:
                        bucket["_won_not_covered"].add(game_id)
                    # Track game tags per game (#148)
                    for tag in game_tags:
                        tag_set = bucket["_tag_game_ids"].get(tag)
                        if tag_set is not None:
                            tag_set.add(game_id)

            # Confidence band: which 10-point band does pct fall in?
            for band in (90, 80, 70, 60, 50):
                if pct >= band:
                    _accum_bucket(by_threshold[str(band)])
                    break

            # Consensus breakdown
            ckey = consensus or ""
            if ckey not in by_consensus:
                by_consensus[ckey] = _new_bucket()
            _accum_bucket(by_consensus[ckey])

            # Quarter breakdown
            qkey = quarter or "unknown"
            if qkey not in by_quarter:
                by_quarter[qkey] = _new_bucket()
            _accum_bucket(by_quarter[qkey])

            # Spread role breakdown (always accumulate, regardless of filter)
            if call_role:
                if call_role not in by_spread_role:
                    by_spread_role[call_role] = _new_bucket()
                _accum_bucket(by_spread_role[call_role])

            # Half breakdown (#349)
            call_half = str(call.get("half") or "").strip().upper()
            if not call_half and quarter:
                call_half = "H1" if quarter in ("Q1", "Q2") else ("H2" if quarter in ("Q3", "Q4") else "OT")
            if call_half:
                if call_half not in by_half:
                    by_half[call_half] = _new_bucket()
                _accum_bucket(by_half[call_half])

            # Edge range breakdown (#349)
            edge_range = _edge_range_key(call.get("avg_edge"))
            if edge_range:
                if edge_range not in by_edge_range:
                    by_edge_range[edge_range] = _new_bucket()
                _accum_bucket(by_edge_range[edge_range])

            # Margin breakdown (#349)
            margin_key = _margin_key(call.get("score_margin_at_fire"))
            if margin_key:
                if margin_key not in by_margin:
                    by_margin[margin_key] = _new_bucket()
                _accum_bucket(by_margin[margin_key])

            # Matrix cross-dimensional pivots (#150, #349)
            if by_matrix is not None:
                # Build dimension values for this call
                thresh_val = ""
                for band in (90, 80, 70, 60, 50):
                    if pct >= band:
                        thresh_val = str(band)
                        break
                dim_vals = {
                    "threshold": thresh_val,
                    "consensus": consensus or "",
                    "quarter": quarter or "unknown",
                    "spread_role": call_role,
                    "half": call_half,
                    "edge_range": edge_range,
                    "margin": margin_key,
                }
                # Check pin filters — skip if call doesn't match any pin
                pin_ok = True
                for pdim, pval in pins.items():
                    if pdim in dim_vals and pval and dim_vals.get(pdim) != pval:
                        pin_ok = False
                        break
                if pin_ok:
                    for dim_a, dim_b in _MATRIX_PAIRS:
                        va, vb = dim_vals[dim_a], dim_vals[dim_b]
                        if not va or not vb:
                            continue  # skip empty dim values
                        pair_key = f"{dim_a}_x_{dim_b}"
                        cell_key = f"{va}|{vb}"
                        bucket_map = by_matrix[pair_key]
                        if cell_key not in bucket_map:
                            bucket_map[cell_key] = _new_bucket()
                        _accum_bucket(bucket_map[cell_key])

                    # Custom compound pivot (#349)
                    if _has_custom:
                        _rv = [dim_vals.get(d, "") for d in _cust_row_dims]
                        _cv = [dim_vals.get(d, "") for d in _cust_col_dims]
                        if all(_rv) and all(_cv):
                            _ckey = "{}|{}".format("~".join(_rv), "~".join(_cv))
                            if _ckey not in by_custom:
                                by_custom[_ckey] = _new_bucket()
                            _accum_bucket(by_custom[_ckey])

        if game_has_resolved_call:
            games_with_calls += 1
        if game_correct:
            games_correct += 1

    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    def _roi_pct(pnl, n):
        return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None

    def _finalize_bucket(d):
        d["pct"] = _pct(d["correct"], d["calls"])
        gc = len(d.pop("_game_ids_correct"))
        d["games"] = len(d.pop("_game_ids"))
        d["games_correct"] = gc
        d["games_won_pct"] = _pct(gc, d["games"])
        d["flat_pnl"] = round(d["flat_pnl"], 2)
        d["flat_roi_pct"] = _roi_pct(d["flat_pnl"], d["calls"])
        d["ml_pnl"] = round(d["ml_pnl"], 2)
        d["ml_roi_pct"] = _roi_pct(d["ml_pnl"], d["calls"])
        # Live line and BK line ROI (#213, #245)
        d["live_line_pnl"] = round(d["live_line_pnl"], 2)
        d["live_line_roi_pct"] = _roi_pct(d["live_line_pnl"], d["live_line_count"]) if d["live_line_count"] > 0 else None
        d["bk_line_pnl"] = round(d["bk_line_pnl"], 2)
        d["bk_line_roi_pct"] = _roi_pct(d["bk_line_pnl"], d["bk_line_count"]) if d["bk_line_count"] > 0 else None
        # BK odds ROI (#299)
        d["bk_odds_pnl"] = round(d["bk_odds_pnl"], 2)
        d["bk_odds_roi_pct"] = _roi_pct(d["bk_odds_pnl"], d["bk_odds_count"]) if d["bk_odds_count"] > 0 else None
        # Spread cover counts and ROI (#148, #153)
        d["spread_cover_yes"] = len(d.pop("_spread_cover_yes"))
        d["spread_cover_no"] = len(d.pop("_spread_cover_no"))
        sc_total = d["spread_cover_yes"] + d["spread_cover_no"]
        d["spread_covered_pct"] = round(d["spread_cover_yes"] / sc_total * 100, 1) if sc_total > 0 else None
        d["spread_roi_pct"] = _roi_pct(
            d["spread_cover_yes"] * FLAT_WIN_PROFIT - d["spread_cover_no"] * STAKE,
            sc_total) if sc_total > 0 else None
        # Covered but didn't win / Won but didn't cover (#153)
        d["covered_not_won"] = len(d.pop("_covered_not_won"))
        d["won_not_covered"] = len(d.pop("_won_not_covered"))
        d["covered_not_won_pct"] = round(d["covered_not_won"] / sc_total * 100, 1) if sc_total > 0 else None
        d["won_not_covered_pct"] = round(d["won_not_covered"] / sc_total * 100, 1) if sc_total > 0 else None
        # Game tag counts (#148)
        tag_counts = {}
        for tag, gids in d.pop("_tag_game_ids").items():
            tag_counts[tag] = len(gids)
        d["tag_counts"] = tag_counts

    for band_data in by_threshold.values():
        _finalize_bucket(band_data)
    for cons_data in by_consensus.values():
        _finalize_bucket(cons_data)
    for q_data in by_quarter.values():
        _finalize_bucket(q_data)
    for sr_data in by_spread_role.values():
        _finalize_bucket(sr_data)
    for h_data in by_half.values():
        _finalize_bucket(h_data)
    for er_data in by_edge_range.values():
        _finalize_bucket(er_data)
    for m_data in by_margin.values():
        _finalize_bucket(m_data)
    if by_matrix is not None:
        for pair_key, bucket_map in by_matrix.items():
            for cell_key in list(bucket_map.keys()):
                _finalize_bucket(bucket_map[cell_key])
    for b in by_custom.values():
        _finalize_bucket(b)

    result_dict = {
        "total_calls":      total_calls,
        "correct":          correct,
        "incorrect":        incorrect,
        "accuracy_pct":     _pct(correct, total_calls),
        "total_games":      games_with_calls,
        "games_correct":    games_correct,
        "games_won_pct":    _pct(games_correct, games_with_calls),
        "pending_calls":    pending_calls,
        "suppressed_calls": suppressed_calls,
        "flat_pnl":         round(flat_pnl, 2),
        "flat_roi_pct":     _roi_pct(flat_pnl, total_calls),
        "ml_pnl":           round(ml_pnl, 2),
        "ml_roi_pct":       _roi_pct(ml_pnl, total_calls),
        "ml_coverage":      ml_coverage,
        "ml_coverage_pct":  _pct(ml_coverage, total_calls),
        "spread_cover_yes": len(_top_spread_cover_yes),
        "spread_cover_no":  len(_top_spread_cover_no),
        "spread_covered_pct": _pct(len(_top_spread_cover_yes), len(_top_spread_cover_yes) + len(_top_spread_cover_no)),
        "spread_roi_pct":   _roi_pct(
            len(_top_spread_cover_yes) * FLAT_WIN_PROFIT - len(_top_spread_cover_no) * STAKE,
            len(_top_spread_cover_yes) + len(_top_spread_cover_no))
            if (len(_top_spread_cover_yes) + len(_top_spread_cover_no)) > 0 else None,
        "covered_not_won":      len(_top_covered_not_won),
        "won_not_covered":      len(_top_won_not_covered),
        "covered_not_won_pct":  _pct(len(_top_covered_not_won), len(_top_spread_cover_yes) + len(_top_spread_cover_no)),
        "won_not_covered_pct":  _pct(len(_top_won_not_covered), len(_top_spread_cover_yes) + len(_top_spread_cover_no)),
        "live_line_pnl":        round(live_line_pnl, 2),
        "live_line_roi_pct":    _roi_pct(live_line_pnl, live_line_count) if live_line_count > 0 else None,
        "live_line_count":      live_line_count,
        "bk_line_pnl":          round(bk_line_pnl, 2),
        "bk_line_roi_pct":      _roi_pct(bk_line_pnl, bk_line_count) if bk_line_count > 0 else None,
        "bk_line_count":        bk_line_count,
        "bk_odds_pnl":          round(bk_odds_pnl, 2),
        "bk_odds_roi_pct":      _roi_pct(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        "bk_odds_count":        bk_odds_count,
        "tag_keys":         all_tag_keys,
        "by_threshold":     by_threshold,
        "by_consensus":     by_consensus,
        "by_quarter":       by_quarter,
        "by_spread_role":   by_spread_role,
        "by_half":          by_half,
        "by_edge_range":    by_edge_range,
        "by_margin":        by_margin,
    }
    if by_matrix is not None:
        result_dict["by_matrix"] = by_matrix
    if _has_custom and by_custom:
        result_dict["by_custom"] = by_custom
    return result_dict


def compute_scenario_stats(records, call_selection="", season_segment="", season_year="",
                           source="", strict_bk=False):
    """
    Compute per-scenario aggregate stats from game_history records (#352).

    Each PW call is categorised into a scenario key (role|margin|edge|quarter)
    using the same bucket definitions as the ROI Matrix. Returns a dict mapping
    scenario_key → stats.

    Stats per scenario:
      accuracy_pct, total_calls, correct, bk_odds_roi_pct, bk_odds_count

    Reuses the same BK odds ROI logic as compute_predicted_winner_accuracy().
    """
    # Filter records by season segment/year if specified
    filtered = _filter_records_by_segment(records, season_segment, season_year)

    norm_call_sel = (call_selection or "").strip().lower()
    norm_source = (source or "").strip().lower()
    scenarios = {}  # scenario_key → stats accumulator

    for rec in filtered:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list) or not calls:
            continue

        spread_fav_id = str(rec.get("spread_fav_id") or "").strip()

        # Exclude suppressed calls, then apply call_selection
        eligible = [c for c in calls if isinstance(c, dict) and not c.get("_suppressed")]
        eligible = _filter_calls_by_selection(eligible, norm_call_sel)
        if not eligible:
            continue

        for call in eligible:
            c = call.get("correct")
            if c is None:
                continue  # skip pending calls

            # Source filter (#371): synthetic=True → backfill, False/missing → live
            if norm_source == "live" and call.get("synthetic"):
                continue
            if norm_source == "backfill" and not call.get("synthetic"):
                continue
            # Live-priced filter (#412 Phase 4.1): exclude backfill-priced calls
            if strict_bk:
                if call.get("synthetic") is True:
                    continue
                if call.get("synthetic") is None and not call.get("bk_ml_source"):
                    continue

            skey = scenario_key_from_call(call, spread_fav_id)
            if not skey:
                continue  # incomplete dimension data

            if skey not in scenarios:
                scenarios[skey] = {
                    "total_calls": 0, "correct": 0, "game_ids": set(),
                    "bk_odds_pnl": 0.0, "bk_odds_count": 0,
                    "bk_spr_pnl": 0.0, "bk_spr_count": 0, "bk_spr_covered": 0,
                    # Quarter/half accumulators (#374)
                    "q_correct": 0, "q_total": 0,
                    "q_ml_pnl": 0.0, "q_ml_count": 0,
                    "q_spr_pnl": 0.0, "q_spr_count": 0, "q_spr_covered": 0,
                    "h1_correct": 0, "h1_total": 0,
                    "h1_ml_pnl": 0.0, "h1_ml_count": 0,
                    "h1_spr_pnl": 0.0, "h1_spr_count": 0, "h1_spr_covered": 0,
                }

            acc = scenarios[skey]
            acc["total_calls"] += 1
            acc["game_ids"].add(str(rec.get("game_id") or ""))
            is_correct = bool(c)
            if is_correct:
                acc["correct"] += 1

            # BK ML ROI (same logic as compute_predicted_winner_accuracy)
            _call_bk_ml = call.get("bk_moneyline")
            if _call_bk_ml is not None:
                try:
                    _bk_ml_int = int(str(_call_bk_ml).replace("+", ""))
                    if _bk_ml_int != 0:
                        acc["bk_odds_count"] += 1
                        acc["bk_odds_pnl"] += _ml_win_profit(_bk_ml_int) if is_correct else -STAKE
                except (ValueError, TypeError):
                    pass

            # BK Spread ROI (#362: requires bk_spread + bk_spread_price, no fallback #412)
            _call_bk_spread = call.get("bk_spread")
            _call_bk_spr_price = call.get("bk_spread_price")
            if _call_bk_spread is not None and _call_bk_spr_price is not None:
                pred_team_id = str(call.get("predicted_team_id") or "").strip()
                _call_bk_cover = _did_team_cover_bk_line(
                    rec, pred_team_id, _call_bk_spread)
                if _call_bk_cover is not None:
                    try:
                        _spr_profit = _ml_win_profit(int(_call_bk_spr_price))
                    except (ValueError, TypeError):
                        _spr_profit = None
                    if _spr_profit is not None:
                        acc["bk_spr_count"] += 1
                        if _call_bk_cover:
                            acc["bk_spr_covered"] += 1
                            acc["bk_spr_pnl"] += _spr_profit
                        else:
                            acc["bk_spr_pnl"] -= STAKE

                    # Quarter/half stats (#374)
                    _q_correct = call.get("q_correct")
                    if _q_correct is not None:
                        acc["q_total"] += 1
                        if _q_correct:
                            acc["q_correct"] += 1

                    _q_ml = call.get("bk_q_ml")
                    if _q_ml is not None:
                        try:
                            _q_ml_int = int(round(float(_q_ml)))
                            acc["q_ml_count"] += 1
                            if _q_correct:
                                acc["q_ml_pnl"] += _ml_win_profit(_q_ml_int)
                            else:
                                acc["q_ml_pnl"] -= STAKE
                        except (ValueError, TypeError):
                            pass

                    _q_spr = call.get("bk_q_spread")
                    _q_covered = call.get("q_covered")
                    if _q_spr is not None and _q_covered is not None:
                        _q_spr_price = call.get("bk_q_spread_price")
                        try:
                            _q_spr_profit = _ml_win_profit(int(round(float(_q_spr_price)))) if _q_spr_price else FLAT_WIN_PROFIT
                        except (ValueError, TypeError):
                            _q_spr_profit = FLAT_WIN_PROFIT
                        acc["q_spr_count"] += 1
                        if _q_covered:
                            acc["q_spr_covered"] += 1
                            acc["q_spr_pnl"] += _q_spr_profit
                        else:
                            acc["q_spr_pnl"] -= STAKE

                    # H1 stats (Q1/Q2 fires only)
                    _h1_correct = call.get("h1_correct")
                    if _h1_correct is not None:
                        acc["h1_total"] += 1
                        if _h1_correct:
                            acc["h1_correct"] += 1

                    _h1_ml = call.get("bk_h1_ml")
                    if _h1_ml is not None:
                        try:
                            _h1_ml_int = int(round(float(_h1_ml)))
                            acc["h1_ml_count"] += 1
                            if _h1_correct:
                                acc["h1_ml_pnl"] += _ml_win_profit(_h1_ml_int)
                            else:
                                acc["h1_ml_pnl"] -= STAKE
                        except (ValueError, TypeError):
                            pass

                    _h1_spr = call.get("bk_h1_spread")
                    _h1_covered = call.get("h1_covered")
                    if _h1_spr is not None and _h1_covered is not None:
                        _h1_spr_price = call.get("bk_h1_spread_price")
                        try:
                            _h1_spr_profit = _ml_win_profit(int(round(float(_h1_spr_price)))) if _h1_spr_price else FLAT_WIN_PROFIT
                        except (ValueError, TypeError):
                            _h1_spr_profit = FLAT_WIN_PROFIT
                        acc["h1_spr_count"] += 1
                        if _h1_covered:
                            acc["h1_spr_covered"] += 1
                            acc["h1_spr_pnl"] += _h1_spr_profit
                        else:
                            acc["h1_spr_pnl"] -= STAKE

    # Finalize: compute derived stats
    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    def _roi_pct(pnl, n):
        return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None

    result = {}
    for skey, acc in scenarios.items():
        result[skey] = {
            "total_calls": acc["total_calls"],
            "total_games": len(acc["game_ids"]),
            "correct": acc["correct"],
            "accuracy_pct": _pct(acc["correct"], acc["total_calls"]),
            "bk_odds_roi_pct": _roi_pct(acc["bk_odds_pnl"], acc["bk_odds_count"]),
            "bk_odds_count": acc["bk_odds_count"],
            "bk_odds_pnl": round(acc["bk_odds_pnl"], 2),
            "bk_spr_roi_pct": _roi_pct(acc["bk_spr_pnl"], acc["bk_spr_count"]),
            "bk_spr_count": acc["bk_spr_count"],
            "bk_spr_covered": acc["bk_spr_covered"],
            "bk_spr_pnl": round(acc["bk_spr_pnl"], 2),
            # Quarter/half stats (#374)
            "q_correct": acc["q_correct"],
            "q_total": acc["q_total"],
            "q_accuracy_pct": _pct(acc["q_correct"], acc["q_total"]),
            "q_covered": acc["q_spr_covered"],
            "q_bk_count": acc["q_spr_count"],
            "q_ml_roi_pct": _roi_pct(acc["q_ml_pnl"], acc["q_ml_count"]),
            "q_ml_count": acc["q_ml_count"],
            "q_spr_roi_pct": _roi_pct(acc["q_spr_pnl"], acc["q_spr_count"]),
            "q_units": round(acc["q_ml_pnl"] / STAKE, 2) if acc["q_ml_count"] else 0,
            "h1_correct": acc["h1_correct"],
            "h1_total": acc["h1_total"],
            "h1_accuracy_pct": _pct(acc["h1_correct"], acc["h1_total"]),
            "h1_covered": acc["h1_spr_covered"],
            "h1_bk_count": acc["h1_spr_count"],
            "h1_ml_roi_pct": _roi_pct(acc["h1_ml_pnl"], acc["h1_ml_count"]),
            "h1_ml_count": acc["h1_ml_count"],
            "h1_spr_roi_pct": _roi_pct(acc["h1_spr_pnl"], acc["h1_spr_count"]),
            "h1_units": round(acc["h1_ml_pnl"] / STAKE, 2) if acc["h1_ml_count"] else 0,
        }
    return result


def _filter_records_by_segment(records, season_segment, season_year=""):
    """Filter game history records by season_segment and/or season_year (#105, #144)."""
    seg = (season_segment or "").strip()
    yr = (season_year or "").strip()
    out = records
    if seg and seg != "all":
        if seg == "regular_season":
            out = [r for r in out if r.get("season_segment") in ("pre_allstar", "post_allstar")]
        elif seg == "playoffs_finals":
            out = [r for r in out if r.get("season_segment") in ("playoffs", "finals")]
        else:
            out = [r for r in out if r.get("season_segment") == seg]
    if yr and yr != "all":
        out = [r for r in out if _game_season_year(r) == yr]
    return out


def _available_seasons(records):
    """Return sorted list of distinct season keys from game records."""
    seasons = set()
    for r in records:
        sy = _game_season_year(r)
        if sy:
            seasons.add(sy)
    return sorted(seasons, reverse=True)


def _recompute_outcomes_background(min_sample, decay_lambda, max_combo_size, season_segment="", spread_role="", season_year="", margin_at_fire=""):
    """
    Background thread target for async outcomes recomputation.
    Acquires lock, computes outcomes, and releases lock.
    Skips recomputation if fingerprint + params are unchanged (issue #130).
    Handles errors gracefully without raising exceptions.
    """
    try:
        with _OUTCOMES_RECOMPUTE_LOCK:
            norm_decay = _normalize_decay_lambda(decay_lambda)
            max_combo_size = max(2, min(5, int(max_combo_size or 3)))  # Clamp to 2-5
            norm_role = (spread_role or "").strip().lower()
            norm_year = (season_year or "").strip()
            norm_maf = (margin_at_fire or "").strip().lower()

            # Skip recomputation if history hasn't changed and params match (issue #130)
            norm_seg = (season_segment or "").strip()
            cache_key = _outcomes_cache_key(norm_seg, norm_role, norm_year, norm_maf)
            slot = _cache_slot(cache_key)
            fingerprint = _history_fingerprint()
            if (
                slot["result"] is not None
                and slot["fingerprint"] == fingerprint
                and slot["min_sample"] == min_sample
                and slot["decay_lambda"] == norm_decay
                and slot.get("max_combo_size") == max_combo_size
                and slot.get("schema_version") == _OUTCOMES_SCHEMA_VERSION
            ):
                slot["no_changes_since"] = datetime.now(timezone.utc).isoformat()
                return

            # Fingerprint or params changed — full recomputation needed
            slot["no_changes_since"] = None
            all_records = load_history(migrate=True)
            records = _filter_records_by_segment(all_records, norm_seg, season_year=norm_year)
            bayes_store = BayesianOutcomeStore.load_or_rebuild(records=records)
            tag_records = [r for r in records if str(r.get("spread_fav_id") or "").strip()] if norm_role else records
            tag_outcomes = compute_tag_outcomes(tag_records, decay_lambda=norm_decay)
            known_tags = sorted(set(tag_outcomes.keys()) | _load_configured_tag_keys())
            base_rates = {
                tag: _to_float_or_none((tag_outcomes.get(tag) or {}).get("rate"))
                for tag in known_tags
            }

            # Guard: refuse to cache empty results when history file has data (#137)
            if not records and _history_file_has_data():
                print(f"[outcomes] BG recompute ({cache_key or 'all'}): load_history returned 0 records but game_history.json has data — skipping cache update", file=sys.stderr)
                return

            games_with_spread = sum(1 for r in records if str(r.get("spread_fav_id") or "").strip()) if norm_role else None
            result = {
                "updated":      datetime.now(timezone.utc).isoformat(),
                "total_games":  len(records),
                "total_games_unfiltered": len(all_records),
                "games_with_spread": games_with_spread,
                "decay_lambda": norm_decay,
                "max_combo_size": max_combo_size,
                "season_segment": norm_seg,
                "spread_role": norm_role,
                "season_year": norm_year,
                "margin_at_fire": norm_maf,
                "available_seasons": _available_seasons(all_records),
                "known_tags":   known_tags,
                "conditions":   compute_condition_stats(records, min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
                "compounds":    compute_compound_stats(records, min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
                "combinations": compute_combination_stats(records, max_combo=max_combo_size, min_sample=min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
                "bayesian_conditions": bayes_store.condition_stats(),
                "tag_outcomes": tag_outcomes,
                "predicted_winner_accuracy": compute_predicted_winner_accuracy(records, spread_role=norm_role, margin_at_fire=norm_maf, matrix=True),
            }
            slot["fingerprint"] = _history_fingerprint()
            slot["min_sample"] = min_sample
            slot["decay_lambda"] = norm_decay
            slot["max_combo_size"] = max_combo_size
            slot["schema_version"] = _OUTCOMES_SCHEMA_VERSION
            slot["last_computed_at"] = time.time()
            slot["result"] = result
            _save_outcomes_cache_to_disk(cache_key)
    except Exception as exc:
        print(f"[outcomes] BG recompute failed: {exc}", file=sys.stderr)


def _outcomes_cache_status(segment_key=""):
    """Return cache status metadata for inclusion in /api/outcomes responses (issue #130)."""
    slot = _OUTCOMES_CACHE.get(segment_key, {})
    last_computed = slot.get("last_computed_at")
    no_changes_since = slot.get("no_changes_since")
    recomputing = not _OUTCOMES_RECOMPUTE_LOCK.acquire(blocking=False)
    if not recomputing:
        _OUTCOMES_RECOMPUTE_LOCK.release()
    return {
        "last_computed_at": datetime.fromtimestamp(last_computed, tz=timezone.utc).isoformat() if last_computed else None,
        "no_changes_since": no_changes_since,
        "recomputing": recomputing,
    }


def _spawn_bg_recompute(min_sample, decay_lambda, max_combo_size, norm_seg, norm_role="", norm_year="", norm_maf=""):
    """Try to spawn a background recompute thread (non-blocking)."""
    can_spawn = _OUTCOMES_RECOMPUTE_LOCK.acquire(blocking=False)
    if can_spawn:
        _OUTCOMES_RECOMPUTE_LOCK.release()
        thread = threading.Thread(
            target=_recompute_outcomes_background,
            args=(min_sample, decay_lambda, max_combo_size, norm_seg, norm_role, norm_year, norm_maf),
            daemon=True
        )
        thread.start()


def compute_outcomes(min_sample=MIN_SAMPLE, decay_lambda=0.0, max_combo_size=3, season_segment="", spread_role="", season_year="",
                     margin_at_fire="", matrix=True, matrix_pins=None):
    """
    Load history and compute both single-condition and combo stats.
    Uses per-segment cache slots so multiple clients viewing different
    segments don't conflict (issue #105).

    spread_role: "" = all, "favorite", "underdog" — filter condition fires (#144)
    season_year: "" = all, "2024-25" etc. — filter by NBA season (#144)
    margin_at_fire: "" = all, "trailing", "leading" — filter by margin at fire (#153)

    Returns dict ready to serve from /api/outcomes.
    """
    norm_decay = _normalize_decay_lambda(decay_lambda)
    max_combo_size = max(2, min(5, int(max_combo_size or 3)))  # Clamp to 2-5
    norm_seg = (season_segment or "").strip()
    norm_role = (spread_role or "").strip().lower()
    norm_year = (season_year or "").strip()
    norm_maf = (margin_at_fire or "").strip().lower()
    cache_key = _outcomes_cache_key(norm_seg, norm_role, norm_year, norm_maf)
    fingerprint = _history_fingerprint()
    now = time.time()
    slot = _cache_slot(cache_key)
    last_computed = slot.get("last_computed_at")

    def _params_match(s):
        return (s["min_sample"] == min_sample
                and s["decay_lambda"] == norm_decay
                and s.get("max_combo_size") == max_combo_size
                and s.get("schema_version") == _OUTCOMES_SCHEMA_VERSION)

    def _bg(ns=norm_seg, nr=norm_role, ny=norm_year, nm=norm_maf):
        _spawn_bg_recompute(min_sample, decay_lambda, max_combo_size, ns, nr, ny, nm)

    def _status():
        return _outcomes_cache_status(cache_key)

    # Cache hit: fingerprint, params, and TTL all match
    if (
        slot["result"] is not None
        and slot["fingerprint"] == fingerprint
        and _params_match(slot)
        and last_computed is not None
        and (now - last_computed) < OUTCOMES_CACHE_TTL
    ):
        result = slot["result"]
        result["cache_status"] = _status()
        return result

    # TTL expired but cache exists → return stale + spawn background refresh
    if (
        slot["result"] is not None
        and slot["fingerprint"] == fingerprint
        and _params_match(slot)
        and last_computed is not None
        and (now - last_computed) >= OUTCOMES_CACHE_TTL
    ):
        _bg()
        result = slot["result"]
        result["cache_status"] = _status()
        return result

    # Fingerprint or params changed but slot has data → return stale + recompute
    if slot["result"] is not None:
        _bg()
        result = slot["result"]
        result["cache_status"] = _status()
        return result

    # Slot empty — try loading from disk (loads even if fingerprint is stale)
    if _load_outcomes_cache_from_disk(cache_key):
        _bg()
        result = slot["result"]
        result["cache_status"] = _status()
        return result

    # Nothing cached for this key.
    # If other slots are already warm, return a placeholder immediately and
    # compute in background so the request doesn't block for minutes.
    any_warm = any(s.get("result") is not None for s in _OUTCOMES_CACHE.values())
    if any_warm:
        _bg()
        # Borrow available_seasons from any warm cache slot (cheap, avoids empty dropdown)
        _placeholder_seasons = []
        for _s in _OUTCOMES_CACHE.values():
            _sr = _s.get("result")
            if isinstance(_sr, dict) and _sr.get("available_seasons"):
                _placeholder_seasons = _sr["available_seasons"]
                break
        pw_empty = {"total_calls": 0, "correct": 0, "incorrect": 0, "accuracy_pct": None, "total_games": 0, "games_correct": 0, "games_won_pct": None, "pending_calls": 0, "suppressed_calls": 0, "by_threshold": {}, "by_consensus": {}, "by_quarter": {}, "by_spread_role": {}}
        placeholder = {
            "updated":      datetime.now(timezone.utc).isoformat(),
            "total_games":  0,
            "decay_lambda": norm_decay,
            "max_combo_size": max_combo_size,
            "season_segment": "",
            "spread_role": norm_role,
            "season_year": norm_year,
            "margin_at_fire": norm_maf,
            "available_seasons": _placeholder_seasons,
            "known_tags":   [],
            "conditions":   {},
            "compounds":    {},
            "combinations": [],
            "bayesian_conditions": {},
            "tag_outcomes": {},
            "predicted_winner_accuracy": pw_empty,
            "cache_status": _status(),
        }
        return placeholder

    # Absolute cold start (no slots cached at all) — compute synchronously
    all_records = load_history(migrate=True)
    records = _filter_records_by_segment(all_records, norm_seg, season_year=norm_year)

    # Guard: refuse to cache empty results when history file has data (#137)
    if not records and _history_file_has_data():
        print(f"[outcomes] Cold-start ({cache_key or 'all'}): load_history returned 0 records but game_history.json has data — returning empty placeholder", file=sys.stderr)
        pw_empty = {"total_calls": 0, "correct": 0, "incorrect": 0, "accuracy_pct": None, "total_games": 0, "games_correct": 0, "games_won_pct": None, "pending_calls": 0, "suppressed_calls": 0, "by_threshold": {}, "by_consensus": {}, "by_quarter": {}, "by_spread_role": {}}
        empty = {
            "updated":      datetime.now(timezone.utc).isoformat(),
            "total_games":  0,
            "decay_lambda": norm_decay,
            "max_combo_size": max_combo_size,
            "season_segment": norm_seg,
            "spread_role": norm_role,
            "season_year": norm_year,
            "margin_at_fire": norm_maf,
            "available_seasons": _available_seasons(all_records),
            "known_tags":   [],
            "conditions":   {},
            "compounds":    {},
            "combinations": [],
            "bayesian_conditions": {},
            "tag_outcomes": {},
            "predicted_winner_accuracy": pw_empty,
            "cache_status": _status(),
        }
        return empty

    bayes_store = BayesianOutcomeStore.load_or_rebuild(records=records)
    tag_records = [r for r in records if str(r.get("spread_fav_id") or "").strip()] if norm_role else records
    tag_outcomes = compute_tag_outcomes(tag_records, decay_lambda=norm_decay)
    known_tags = sorted(set(tag_outcomes.keys()) | _load_configured_tag_keys())
    base_rates = {
        tag: _to_float_or_none((tag_outcomes.get(tag) or {}).get("rate"))
        for tag in known_tags
    }

    games_with_spread = sum(1 for r in records if str(r.get("spread_fav_id") or "").strip()) if norm_role else None
    result = {
        "updated":      datetime.now(timezone.utc).isoformat(),
        "total_games":  len(records),
        "total_games_unfiltered": len(all_records),
        "games_with_spread": games_with_spread,
        "decay_lambda": norm_decay,
        "max_combo_size": max_combo_size,
        "season_segment": norm_seg,
        "spread_role": norm_role,
        "season_year": norm_year,
        "margin_at_fire": norm_maf,
        "available_seasons": _available_seasons(all_records),
        "known_tags":   known_tags,
        "conditions":   compute_condition_stats(records, min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
        "compounds":    compute_compound_stats(records, min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
        "combinations": compute_combination_stats(records, max_combo=max_combo_size, min_sample=min_sample, decay_lambda=norm_decay, known_tags=known_tags, base_rates=base_rates, spread_role=norm_role, margin_at_fire=norm_maf),
        "bayesian_conditions": bayes_store.condition_stats(),
        "tag_outcomes": tag_outcomes,
        "predicted_winner_accuracy": compute_predicted_winner_accuracy(records, spread_role=norm_role, margin_at_fire=norm_maf, matrix=matrix, matrix_pins=matrix_pins),
    }
    slot["fingerprint"] = fingerprint
    slot["min_sample"] = min_sample
    slot["decay_lambda"] = norm_decay
    slot["max_combo_size"] = max_combo_size
    slot["schema_version"] = _OUTCOMES_SCHEMA_VERSION
    slot["last_computed_at"] = now
    slot["no_changes_since"] = None
    slot["result"] = result
    _save_outcomes_cache_to_disk(cache_key)
    result["cache_status"] = _status()
    return result


# ── Live analysis ─────────────────────────────────────────────────────────────

def _cap_predictions(predictions, max_non_ml=10, max_ml=5):
    """
    Split predictions into ML (basis='ml') and non-ML buckets, cap each independently,
    then re-sort and return combined list.
    ML rows are reserved their own cap so they can't be crowded out by combo rows.
    """
    ml_rows     = [p for p in predictions if p.get("basis") == "ml"]
    non_ml_rows = [p for p in predictions if p.get("basis") != "ml"]
    capped = non_ml_rows[:max_non_ml] + ml_rows[:max_ml]
    # Re-sort: combo first, then by pct descending, ML rows go last within their pct group
    capped.sort(key=lambda x: (
        0 if x.get("basis") == "combo" else (2 if x.get("basis") == "ml" else 1),
        -float(x.get("pct") or 0),
    ))
    return capped


def build_condition_index(records):
    """
    Pre-build inverted index for a record list.
    Returns (cond_team_idx, cond_idx, rec_list) that can be passed to
    live_analysis_for_game() via _prebuilt_index to avoid per-call rebuild.
    """
    from collections import defaultdict
    rec_list = list(records)
    cond_team_idx = defaultdict(set)  # (cond, tid) -> {rec_idx}
    cond_idx = defaultdict(set)       # cond -> {rec_idx}
    for ri, rec in enumerate(rec_list):
        for cf in (rec.get("conditions_fired") or []):
            if not isinstance(cf, dict):
                continue
            tid = str(cf.get("team_id") or "").strip()
            cn  = str(cf.get("name") or "").strip()
            if tid and cn:
                cond_team_idx[(cn, tid)].add(ri)
                cond_idx[cn].add(ri)
    return (cond_team_idx, cond_idx, rec_list)


def live_analysis_for_game(game_id, active_conditions, records, min_sample=MIN_SAMPLE, active_hits=None, decay_lambda=0.0, _prebuilt_index=None, config=None):
    """
    Given the conditions currently active in a live game, use historical data
    to compute outcome probabilities.

        active_conditions: list of condition name strings currently firing
        active_hits: list of dicts with current team-level hits, each containing:
            - team (display name)
            - team_id
            - condition
    Returns:
    {
      "active_conditions": [...],
      "predictions": [
        { "label": "UPSET",    "pct": 83.0, "sample": 6, "basis": "combo" },
        { "label": "CLOSE GAME","pct": 61.0, "sample": 4, "basis": "single" },
        { "label": "Team wins", "pct": 55.0, "sample": 9, "basis": "single" },
      ]
    }
    """
    ml_status = {
        "enabled": ml_model is not None,
        "ok": False,
        "reason": "module_unavailable" if ml_model is None else "not_attempted",
        "feature_schema_version": None,
        "trained_at": None,
        "predictions_added": 0,
    }

    # Apply config-based Bayesian priors (#200 Phase 2)
    apply_bayes_priors_from_config(config)

    condition_team_map = {}
    for hit in active_hits or []:
        cond_name = str(hit.get("condition") or "").strip()
        if not cond_name:
            continue
        team_name = str(hit.get("team") or "").strip()
        team_id = str(hit.get("team_id") or "").strip()
        team_label = team_name or team_id
        if not team_label:
            continue
        condition_team_map.setdefault(cond_name, set()).add(team_label)

    active_conditions_detailed = []
    for cond_name in active_conditions or []:
        teams = sorted(condition_team_map.get(str(cond_name), set()))
        active_conditions_detailed.append({
            "condition": cond_name,
            "teams": teams,
        })

    # Configurable Bayesian blending thresholds (#195.5)
    _blend_cfg = ((config or {}).get("pw_blending") or {}) if isinstance((config or {}).get("pw_blending"), dict) else {}
    _blend_t1 = float(_blend_cfg.get("sample_threshold_low", 15))
    _blend_t2 = float(_blend_cfg.get("sample_threshold_high", 50))
    _blend_w_low = float(_blend_cfg.get("bayes_weight_low", 0.7))
    _blend_w_mid = float(_blend_cfg.get("bayes_weight_mid", 0.5))
    _blend_w_high = float(_blend_cfg.get("bayes_weight_high", 0.2))

    def _winner_score_pct(freq_pct, bayes_pct, effective_sample, basis):
        """Blend frequency and Bayes winner score for single-condition rows only."""
        try:
            freq = float(freq_pct)
        except (TypeError, ValueError):
            return None
        if str(basis or "") != "single":
            return round(freq, 1)
        if bayes_pct is None:
            return round(freq, 1)
        try:
            bayes = float(bayes_pct)
        except (TypeError, ValueError):
            return round(freq, 1)
        try:
            eff_n = float(effective_sample)
        except (TypeError, ValueError):
            eff_n = 0.0

        if eff_n < _blend_t1:
            bayes_w = _blend_w_low
        elif eff_n < _blend_t2:
            bayes_w = _blend_w_mid
        else:
            bayes_w = _blend_w_high
        freq_w = 1.0 - bayes_w
        return round((freq * freq_w) + (bayes * bayes_w), 1)

    if not active_conditions or not records:
        ml_status["reason"] = "insufficient_live_context"
        return {
            "active_conditions": active_conditions,
            "active_conditions_detailed": active_conditions_detailed,
            "predictions": [],
            "ml_status": ml_status,
        }

    active_set = set(active_conditions)
    predictions = []
    seen_labels = set()
    now_utc = datetime.now(timezone.utc)
    bayes_store = BayesianOutcomeStore.load_or_rebuild(records=records)

    # ── Contextual weighting (issue #73 Phase 1) ─────────────────────────
    # Build per-condition current-game context from active_hits so we can
    # boost historical matches that fired in a similar situation (margin +
    # quarter progression).  Uses the FIRST hit per condition name as the
    # representative context (most recent fire for that condition).
    _cfg = config or {}
    _ctx_margin_band = float(_cfg.get("contextual_margin_band", 8))
    _ctx_quarter_band = float(_cfg.get("contextual_quarter_band", 0.15))
    _ctx_boost_max = float(_cfg.get("contextual_boost_max", 1.5))

    _current_ctx = {}  # {cond_name: {"margin": float|None, "qpct": float|None}}
    for hit in active_hits or []:
        cn = str(hit.get("condition") or "").strip()
        if not cn or cn in _current_ctx:
            continue
        _current_ctx[cn] = {
            "margin": _to_float_or_none(hit.get("score_margin_at_fire")),
            "qpct": _to_float_or_none(hit.get("quarter_elapsed_pct")),
        }

    def _contextual_boost(record, team_id, combo_key):
        """Compute a multiplicative contextual similarity boost (1.0–boost_max).

        For each condition in combo_key, finds the matching conditions_fired
        entry and compares its score_margin_at_fire and quarter_elapsed_pct
        against the current game's active_hits context. Each dimension that
        falls within its band contributes +0.5*(boost_max-1) to the boost.
        Returns 1.0 (no boost) when context data is unavailable.
        """
        if not _current_ctx or _ctx_boost_max <= 1.0:
            return 1.0
        boost_parts = []
        for cn in combo_key:
            cur = _current_ctx.get(cn)
            if not cur:
                continue
            # Find the historical condition entry for this team + condition
            hist_cf = None
            for cf in record.get("conditions_fired") or []:
                if (str(cf.get("name") or "").strip() == cn
                        and str(cf.get("team_id") or "").strip() == str(team_id)):
                    hist_cf = cf
                    break
            if hist_cf is None:
                continue
            # Margin similarity
            cur_m = cur["margin"]
            hist_m = _to_float_or_none(hist_cf.get("score_margin_at_fire"))
            if cur_m is not None and hist_m is not None and _ctx_margin_band > 0:
                if abs(cur_m - hist_m) < _ctx_margin_band:
                    boost_parts.append(1)
            # Quarter-elapsed similarity (stored as 0–100 percentage)
            cur_q = cur["qpct"]
            hist_q = _to_float_or_none(hist_cf.get("quarter_elapsed_pct"))
            if cur_q is not None and hist_q is not None and _ctx_quarter_band > 0:
                # Normalize to 0–1 for band comparison
                diff = abs((cur_q / 100.0) - (hist_q / 100.0))
                if diff < _ctx_quarter_band:
                    boost_parts.append(1)
        if not boost_parts:
            return 1.0
        # Each matching dimension contributes an equal share of (boost_max - 1)
        max_dims = len(combo_key) * 2  # 2 dimensions per condition
        per_dim = (_ctx_boost_max - 1.0) / max(max_dims, 1)
        return 1.0 + per_dim * len(boost_parts)

    team_ctx = {}
    for hit in active_hits or []:
        cond_name = str(hit.get("condition", "")).strip()
        if not cond_name:
            continue
        team_name = str(hit.get("team", "")).strip()
        team_id = str(hit.get("team_id", "")).strip()
        team_score = _to_int_or_none(hit.get("score"))
        if not team_name and not team_id:
            continue
        key = team_id or team_name.lower()
        if key not in team_ctx:
            team_ctx[key] = {
                "team_id": team_id,
                "team_name": team_name,
                "score": team_score,
                "home_away_role": str(hit.get("home_away_role", "")).strip().lower(),
                "conds": set(),
            }
        if team_ctx[key].get("score") is None and team_score is not None:
            team_ctx[key]["score"] = team_score
        if not team_ctx[key].get("home_away_role"):
            team_ctx[key]["home_away_role"] = str(hit.get("home_away_role", "")).strip().lower()
        team_ctx[key]["conds"].add(cond_name)

    # Use pre-built index if provided, otherwise build it now (fallback for
    # callers that don't pre-build, e.g. live monitor).
    if _prebuilt_index is not None:
        _cond_team_idx, _cond_idx, _rec_list = _prebuilt_index
    else:
        _cond_team_idx = defaultdict(set)
        _rec_list = list(records)
        for _ri, _rec in enumerate(_rec_list):
            for _cf in (_rec.get("conditions_fired") or []):
                if not isinstance(_cf, dict):
                    continue
                _tid = str(_cf.get("team_id") or "").strip()
                _cn  = str(_cf.get("name") or "").strip()
                if _tid and _cn:
                    _cond_team_idx[(_cn, _tid)].add(_ri)
        _cond_idx = defaultdict(set)
        for (_cn, _tid), _idxs in _cond_team_idx.items():
            _cond_idx[_cn].update(_idxs)

    # Collect suppressed "Team wins" rows for filter logging (issue #152).
    # Only the top suppressed row is logged — avoids flooding with per-combo lines.
    _pw_suppressed_rows = []

    # Try largest matching combo first, then singles
    for size in range(min(3, len(active_conditions)), 0, -1):
        for combo in combinations(sorted(active_set), size):
            combo_key = tuple(sorted(combo))
            if size > 1:
                # Combo predictions are valid only when active conditions co-fire for one team context.
                has_active_same_team_combo = any(
                    all(c in team.get("conds", set()) for c in combo_key)
                    for team in team_ctx.values()
                )
                if not has_active_same_team_combo:
                    continue

            # Find historical games where ALL these conditions fired for the same team.
            # Use index intersection: candidate record indices = intersection of per-condition sets.
            matching = []
            # Get all team_ids that appear in the index for the first condition
            first_cond = combo_key[0]
            candidate_teams = {tid for (cn, tid) in _cond_team_idx if cn == first_cond}
            for tid in candidate_teams:
                # Intersect record-index sets across all conditions in combo for this team
                idx_sets = [_cond_team_idx.get((cn, tid), set()) for cn in combo_key]
                shared = idx_sets[0]
                for s in idx_sets[1:]:
                    shared = shared & s
                for ri in shared:
                    matching.append({
                        "record": _rec_list[ri],
                        "team_id": tid,
                    })

            n = len(matching)
            if n < min_sample:
                continue

            match_rows = [
                {
                    "record": row["record"],
                    "team_id": row.get("team_id"),
                    "weight": (_recency_weight(row["record"], decay_lambda, now_utc)
                               * _contextual_boost(row["record"], row.get("team_id"), combo_key)),
                }
                for row in matching
            ]
            total_weight = sum(row["weight"] for row in match_rows if row["weight"] > 0)
            if total_weight <= 0 or total_weight < float(min_sample):
                continue

            basis = "combo" if size > 1 else "single"
            label_prefix = " + ".join(combo_key) if size > 1 else combo_key[0]

            for outcome_key, outcome_label in [
                ("was_upset",    "UPSET"),
                ("was_close",    "Close game"),
                ("was_comeback", "Q4 Comeback"),
                ("was_blowout",  "Blowout"),
            ]:
                full_label = "{}: {}".format(label_prefix, outcome_label) if size > 1 else outcome_label
                if full_label in seen_labels:
                    continue
                eligible_rows = match_rows
                eligible_weight = total_weight
                if outcome_key == "was_upset":
                    eligible_rows = [
                        row for row in match_rows
                        if _normalize_bool_or_none(row["record"].get("was_upset")) is not None
                    ]
                    eligible_weight = sum(row["weight"] for row in eligible_rows if row["weight"] > 0)
                    if eligible_weight < float(min_sample):
                        continue

                pct = round(
                    sum(row["weight"] for row in eligible_rows if row["record"].get(outcome_key)) / eligible_weight * 100,
                    1,
                )
                if pct > 0:
                    payload = {
                        "label":   full_label,
                        "pct":     pct,
                        "sample":  n,
                        "effective_sample": round(eligible_weight, 1),
                        "basis":   basis,
                        "conditions": list(combo_key),
                    }
                    bayes_pct, bayes_sample = bayes_store.combined_probability(combo_key, outcome_key.replace("was_", "game_"))
                    if bayes_pct is not None:
                        payload["bayes_pct"] = bayes_pct
                        payload["bayes_sample"] = bayes_sample
                    predictions.append(payload)
                    seen_labels.add(full_label)

            # Team wins (based on conditions that fired for a specific team)
            tw_label = "Team wins ({})".format(label_prefix) if size > 1 else "Team wins"
            if tw_label not in seen_labels:
                tw_weight_rows = [
                    {
                        "team_won": bool(cf.get("team_won")),
                        "weight": row["weight"],
                        "home_away_role": str(cf.get("home_away_role") or "").strip().lower(),
                    }
                    for row in match_rows
                    for cf in row["record"].get("conditions_fired", [])
                    if str(cf.get("team_id") or "").strip() == str(row.get("team_id") or "").strip()
                    and cf.get("name","") in combo_key
                ]
                if tw_weight_rows:
                    tw_pct = _weighted_mean(tw_weight_rows, "team_won")
                    predicted_team = None
                    predicted_team_id = None

                    candidates = [
                        team for team in team_ctx.values()
                        if all(c in team.get("conds", set()) for c in combo_key)
                    ]
                    if candidates:
                        # Prefer historically winning side alignment before live-score tiebreakers.
                        role_win_total = {"home": 0.0, "away": 0.0}
                        role_total = {"home": 0.0, "away": 0.0}
                        for row in tw_weight_rows:
                            role = str(row.get("home_away_role") or "").strip().lower()
                            if role not in ("home", "away"):
                                continue
                            w = float(row.get("weight") or 0.0)
                            if w <= 0:
                                continue
                            role_total[role] += w
                            if row.get("team_won"):
                                role_win_total[role] += w

                        role_win_rate = {}
                        for role in ("home", "away"):
                            if role_total[role] > 0:
                                role_win_rate[role] = role_win_total[role] / role_total[role]

                        preferred_role = None
                        if role_win_rate:
                            preferred_role = max(role_win_rate.items(), key=lambda kv: kv[1])[0]

                        filtered_candidates = [
                            t for t in candidates
                            if str(t.get("home_away_role") or "").strip().lower() == preferred_role
                        ] if preferred_role else []
                        if filtered_candidates:
                            candidates = filtered_candidates

                        candidates.sort(
                            key=lambda t: (
                                t.get("score") is not None,
                                _to_int_or_none(t.get("score")) if t.get("score") is not None else -1,
                                len(t.get("conds", [])),
                                str(t.get("team_name", "")),
                            ),
                            reverse=True,
                        )
                        top = candidates[0]
                        predicted_team = str(top.get("team_name", "")).strip() or str(top.get("team_id", "")).strip() or None
                        predicted_team_id = str(top.get("team_id", "")).strip() or None

                    payload = {
                        "label":   tw_label,
                        "pct":     tw_pct,
                        "sample":  n,
                        "effective_sample": round(total_weight, 1),
                        "basis":   basis,
                        "conditions": list(combo_key),
                    }
                    if predicted_team:
                        payload["predicted_team"] = predicted_team
                    if predicted_team_id:
                        payload["predicted_team_id"] = predicted_team_id
                    bayes_pct, bayes_sample = bayes_store.combined_probability(combo_key, "team_wins")
                    if bayes_pct is not None:
                        payload["bayes_pct"] = bayes_pct
                        payload["bayes_sample"] = bayes_sample
                    winner_score_pct = _winner_score_pct(
                        freq_pct=payload.get("pct"),
                        bayes_pct=payload.get("bayes_pct"),
                        effective_sample=payload.get("effective_sample"),
                        basis=payload.get("basis"),
                    )
                    # ── PW confidence modifiers (issue #195) ─────────
                    # All modifiers are multiplicative and config-driven.
                    # Set any modifier to 1.0 (or false) to disable.
                    if winner_score_pct is not None:
                        _pw_mod = 1.0

                        # Modifier: quarter-weighted confidence (#195.4)
                        _pw_qw = _cfg.get("pw_quarter_weights")
                        if isinstance(_pw_qw, dict):
                            # Determine current quarter from active hits
                            _mod_quarter = ""
                            for h in active_hits or []:
                                if str(h.get("team_id", "")).strip() == (predicted_team_id or ""):
                                    _mod_quarter = str(h.get("quarter_str") or h.get("quarter") or "").strip()
                                    if _mod_quarter:
                                        break
                            if not _mod_quarter:
                                for h in active_hits or []:
                                    _mod_quarter = str(h.get("quarter_str") or h.get("quarter") or "").strip()
                                    if _mod_quarter:
                                        break
                            _qw_val = _to_float_or_none(_pw_qw.get(_mod_quarter))
                            if _qw_val is not None:
                                _pw_mod *= _qw_val

                        # Modifier: score margin boost (#195.3)
                        if predicted_team_id:
                            _mod_margins = [
                                float(h.get("score_margin_at_fire"))
                                for h in active_hits or []
                                if str(h.get("team_id", "")).strip() == predicted_team_id
                                and h.get("score_margin_at_fire") is not None
                            ]
                            _mod_avg_margin = (sum(_mod_margins) / len(_mod_margins)) if _mod_margins else None
                            if _mod_avg_margin is not None and _mod_avg_margin > 0:
                                _mod_quarter_for_margin = ""
                                for h in active_hits or []:
                                    if str(h.get("team_id", "")).strip() == predicted_team_id:
                                        _mod_quarter_for_margin = str(h.get("quarter_str") or h.get("quarter") or "").strip()
                                        if _mod_quarter_for_margin:
                                            break
                                if _mod_quarter_for_margin in ("Q3", "Q4", "OT"):
                                    _pw_mod *= float(_cfg.get("pw_margin_boost_leading_late", 1.0))
                                else:
                                    _pw_mod *= float(_cfg.get("pw_margin_boost_leading", 1.0))

                        # Modifier: condition quality scoring (#195.8)
                        if _cfg.get("pw_condition_quality_enabled", False) and predicted_team_id:
                            _cq_weights = _cfg.get("pw_condition_quality_weights")
                            if isinstance(_cq_weights, dict):
                                _cq_tc = team_ctx.get(predicted_team_id, {})
                                _cq_conds = _cq_tc.get("conds", set())
                                _cq_total = 0.0
                                _cq_count = 0
                                for _cq_c in _cq_conds:
                                    _cq_w = _to_float_or_none(_cq_weights.get(_cq_c))
                                    if _cq_w is not None:
                                        _cq_total += _cq_w
                                        _cq_count += 1
                                if _cq_count > 0:
                                    _cq_avg = _cq_total / _cq_count
                                    _pw_mod *= _cq_avg

                        # Apply combined modifier (clamp to 0-100 range)
                        if _pw_mod != 1.0:
                            winner_score_pct = max(0.0, min(100.0, round(winner_score_pct * _pw_mod, 1)))

                        payload["winner_score_pct"] = winner_score_pct

                    # ── Predicted winner quality filters (issue #152) ────
                    # Suppress "Team wins" rows that are historically
                    # unreliable: teams trailing heavily or with net
                    # negative condition polarity.  Log suppressed
                    # rows so they can be reviewed without affecting
                    # predictions.
                    _pw_suppressed = False
                    _pw_suppress_reason = ""
                    _pw_neg = 0
                    _pw_pos = 0
                    _pw_net = 0
                    if predicted_team_id:
                        _pw_team_key = predicted_team_id or (predicted_team or "").lower()
                        _pw_tc = team_ctx.get(_pw_team_key, {})
                        _pw_conds = _pw_tc.get("conds", set())

                        # Always compute polarity for payload
                        _pw_neg = sum(1 for c in _pw_conds if classify_polarity(c) == "neg")
                        _pw_pos = sum(1 for c in _pw_conds if classify_polarity(c) == "pos")
                        _pw_net = _pw_pos - _pw_neg

                        # Filter A: margin gate
                        _pw_margins = [
                            float(h.get("score_margin_at_fire"))
                            for h in active_hits or []
                            if str(h.get("team_id", "")).strip() == predicted_team_id
                            and h.get("score_margin_at_fire") is not None
                        ]
                        _pw_avg_margin = (sum(_pw_margins) / len(_pw_margins)) if _pw_margins else None
                        _pw_quarter = ""
                        for h in active_hits or []:
                            if str(h.get("team_id", "")).strip() == predicted_team_id:
                                _pw_quarter = str(h.get("quarter_str") or h.get("quarter") or "").strip()
                                if _pw_quarter:
                                    break

                        if _pw_avg_margin is not None:
                            _mg_full = (config or {}).get("pw_margin_gate", -15)
                            _mg_late = (config or {}).get("pw_margin_gate_late", -10)
                            if _pw_avg_margin <= _mg_full:
                                _pw_suppressed = True
                                _pw_suppress_reason = "margin_gate: trailing by {:.0f}".format(abs(_pw_avg_margin))
                            elif _pw_avg_margin <= _mg_late and _pw_quarter in ("Q3", "Q4", "OT"):
                                _pw_suppressed = True
                                _pw_suppress_reason = "margin_gate: trailing by {:.0f} in {}".format(abs(_pw_avg_margin), _pw_quarter)

                        # Filter C: polarity strict (net polarity >= threshold, default 0; None=disabled)
                        if not _pw_suppressed:
                            _pw_polarity_min = (config or {}).get("pw_polarity_gate", 0)
                            if _pw_polarity_min is not None and _pw_net < _pw_polarity_min:
                                _pw_suppressed = True
                                _pw_suppress_reason = "polarity_gate: net={:+d} (pos={}, neg={})".format(_pw_net, _pw_pos, _pw_neg)

                        # Always compute avg_edge for persistence (#248); apply edge gate suppression
                        # only when pw_edge_gate is configured and call not already suppressed.
                        _pw_avg_edge = None
                        try:
                            _edge_store = ConditionEdgeStore.load_or_rebuild(records=records)
                            _edge_cond_team = {}
                            for h in active_hits or []:
                                _ecn = str(h.get("condition") or "").strip()
                                _ect = str(h.get("team_id") or "").strip()
                                if _ecn and _ect:
                                    _edge_cond_team[_ecn] = _ect
                            _pw_avg_edge = _edge_store.compute_avg_edge(
                                [{"name": c} for c in combo_key],
                                predicted_team_id, _edge_cond_team)
                        except Exception:
                            pass
                        # Filter D: edge gate (#217)
                        _pw_edge_min = (config or {}).get("pw_edge_gate")
                        if not _pw_suppressed and _pw_edge_min is not None and _pw_avg_edge is not None and _pw_avg_edge < float(_pw_edge_min):
                            _pw_suppressed = True
                            _pw_suppress_reason = "edge_gate: avg_edge={:+.1f} (threshold={})".format(_pw_avg_edge, _pw_edge_min)

                    # Attach polarity and edge data to all Team wins payloads
                    payload["polarity_pos"] = _pw_pos
                    payload["polarity_neg"] = _pw_neg
                    payload["polarity_net"] = _pw_net
                    if _pw_avg_edge is not None:
                        payload["avg_edge"] = _pw_avg_edge

                    if _pw_suppressed:
                        payload["_suppressed"] = True
                        payload["_suppress_reason"] = _pw_suppress_reason
                        _pw_suppressed_rows.append(payload)
                    else:
                        predictions.append(payload)
                    seen_labels.add(tw_label)

    # ── Filter log: only when a suppression would have been the top PW call ──
    # Find the highest-scoring suppressed "Team wins" row and compare against
    # the highest-scoring kept row.  Log only if the suppressed row would have
    # outscored (or tied) the best kept row — i.e. the suppression changed the
    # predicted winner outcome.
    if _pw_suppressed_rows:
        def _pw_row_score(row):
            return float(row.get("winner_score_pct") or row.get("pct") or 0)

        _top_suppressed = max(_pw_suppressed_rows, key=_pw_row_score)
        _kept_tw = [p for p in predictions if "team wins" in str(p.get("label", "")).lower()]
        _top_kept_score = max((_pw_row_score(p) for p in _kept_tw), default=0)

        if _pw_row_score(_top_suppressed) >= _top_kept_score:
            try:
                _s_team = _top_suppressed.get("predicted_team") or "?"
                _s_pct = _pw_row_score(_top_suppressed)
                _s_basis = _top_suppressed.get("basis", "")
                _s_reason = _top_suppressed.get("_suppress_reason", "")
                _s_n = len(_pw_suppressed_rows)
                _ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                print("{} [PW_FILTER] SUPPRESSED: {} {:.0f}% ({}) — {} [{} rows total]".format(
                    _ts, _s_team, _s_pct, _s_basis, _s_reason, _s_n,
                ), file=sys.stderr)
            except Exception:
                pass

    # Optional Phase 3A model predictions (fail closed to existing frequency/Bayesian output).
    if ml_model is not None:
        try:
            ml_result = ml_model.predict_live(active_hits or [])
            ml_status["feature_schema_version"] = ml_result.get("feature_schema_version")
            ml_status["trained_at"] = ml_result.get("trained_at")
            if ml_result.get("ok"):
                added = 0
                for row in ml_result.get("predictions", []):
                    if not isinstance(row, dict):
                        continue
                    label = str(row.get("label") or "").strip()
                    if not label or label in seen_labels:
                        continue
                    pct = _to_float_or_none(row.get("pct"))
                    if pct is None:
                        continue
                    payload = {
                        "label": label,
                        "pct": round(pct, 1),
                        "sample": 0,
                        "effective_sample": 0.0,
                        "basis": "ml",
                        "ml_variant": str(row.get("ml_variant") or "condition_only"),
                    }
                    predicted_team = str(row.get("predicted_team") or "").strip()
                    predicted_team_id = str(row.get("predicted_team_id") or "").strip()
                    if predicted_team:
                        payload["predicted_team"] = predicted_team
                    if predicted_team_id:
                        payload["predicted_team_id"] = predicted_team_id

                    # Apply margin gate to ML "Team wins" predictions (#205)
                    _ml_suppressed = False
                    _ml_suppress_reason = ""
                    _ml_pol_pos = 0
                    _ml_pol_neg = 0
                    _ml_pol_net = 0
                    _ml_avg_edge = None
                    if "team wins" in label.lower() and predicted_team_id:
                        _ml_margins = [
                            float(h.get("score_margin_at_fire"))
                            for h in active_hits or []
                            if str(h.get("team_id", "")).strip() == predicted_team_id
                            and h.get("score_margin_at_fire") is not None
                        ]
                        _ml_avg_margin = (sum(_ml_margins) / len(_ml_margins)) if _ml_margins else None
                        if _ml_avg_margin is not None:
                            _ml_mg_full = (config or {}).get("pw_margin_gate", -15)
                            _ml_mg_late = (config or {}).get("pw_margin_gate_late", -10)
                            _ml_quarter = ""
                            for h in active_hits or []:
                                if str(h.get("team_id", "")).strip() == predicted_team_id:
                                    _ml_quarter = str(h.get("quarter_str") or h.get("quarter") or "").strip()
                                    if _ml_quarter:
                                        break
                            if _ml_avg_margin <= _ml_mg_full:
                                _ml_suppressed = True
                                _ml_suppress_reason = "ml_margin_gate: trailing by {:.0f}".format(abs(_ml_avg_margin))
                            elif _ml_avg_margin <= _ml_mg_late and _ml_quarter in ("Q3", "Q4", "OT"):
                                _ml_suppressed = True
                                _ml_suppress_reason = "ml_margin_gate: trailing by {:.0f} in {}".format(abs(_ml_avg_margin), _ml_quarter)

                        # Filter C: polarity gate for ML predictions (#221)
                        _ml_tc = team_ctx.get(predicted_team_id, {})
                        _ml_conds = _ml_tc.get("conds", set())
                        _ml_pol_neg = sum(1 for c in _ml_conds if classify_polarity(c) == "neg")
                        _ml_pol_pos = sum(1 for c in _ml_conds if classify_polarity(c) == "pos")
                        _ml_pol_net = _ml_pol_pos - _ml_pol_neg
                        if not _ml_suppressed:
                            _ml_polarity_min = (config or {}).get("pw_polarity_gate", 0)
                            if _ml_polarity_min is not None and _ml_pol_net < _ml_polarity_min:
                                _ml_suppressed = True
                                _ml_suppress_reason = "polarity_gate: net={:+d} (pos={}, neg={})".format(_ml_pol_net, _ml_pol_pos, _ml_pol_neg)

                        # Always compute avg_edge for persistence (#248)
                        try:
                            _ml_edge_store = ConditionEdgeStore.load_or_rebuild(records=records)
                            _ml_edge_cond_team = {}
                            for h in active_hits or []:
                                _ecn = str(h.get("condition") or "").strip()
                                _ect = str(h.get("team_id") or "").strip()
                                if _ecn and _ect:
                                    _ml_edge_cond_team[_ecn] = _ect
                            _ml_avg_edge = _ml_edge_store.compute_avg_edge(
                                [{"name": c} for c in _ml_conds],
                                predicted_team_id, _ml_edge_cond_team)
                        except Exception:
                            pass
                        # Filter D: edge gate for ML predictions (#221)
                        _ml_edge_min = (config or {}).get("pw_edge_gate")
                        if not _ml_suppressed and _ml_edge_min is not None and _ml_avg_edge is not None and _ml_avg_edge < float(_ml_edge_min):
                            _ml_suppressed = True
                            _ml_suppress_reason = "edge_gate: avg_edge={:+.1f} (threshold={})".format(_ml_avg_edge, _ml_edge_min)

                        if _ml_suppressed:
                            payload["_suppressed"] = True
                            payload["_suppress_reason"] = _ml_suppress_reason
                            _pw_suppressed_rows.append(payload)

                    # Attach polarity and edge data to ML payload
                    payload["polarity_pos"] = _ml_pol_pos
                    payload["polarity_neg"] = _ml_pol_neg
                    payload["polarity_net"] = _ml_pol_net
                    if _ml_avg_edge is not None:
                        payload["avg_edge"] = _ml_avg_edge

                    if not _ml_suppressed:
                        predictions.append(payload)
                    seen_labels.add(label)
                    added += 1
                ml_status["ok"] = True
                ml_status["reason"] = "ready"
                ml_status["predictions_added"] = added
            else:
                ml_status["ok"] = False
                ml_status["reason"] = str(ml_result.get("error") or "not_ready")
        except Exception:
            ml_status["ok"] = False
            ml_status["reason"] = "runtime_error"

    # Sort: combo first, then highest pct
    predictions.sort(key=lambda x: (-1 if x["basis"] == "combo" else 0, -x["pct"]))
    
    # ── Infer predicted winner from upset probability when no team_wins exists
    # If we have an UPSET prediction but no Team wins prediction, infer winner from:
    # upset_pct > 50% means underdog won → predicted winner is underdog
    # upset_pct <= 50% means favorite won → predicted winner is favorite
    has_team_wins = any("team wins" in str(p.get("label", "")).lower() for p in predictions)
    has_upset = any("upset" in str(p.get("label", "")).lower() for p in predictions if p.get("basis") == "single")
    
    if not has_team_wins and has_upset:
        upset_preds = [p for p in predictions if "upset" in str(p.get("label", "")).lower() and p.get("basis") == "single"]
        if upset_preds:
            upset_pred = upset_preds[0]  # Take the highest pct (already sorted)
            upset_pct = float(upset_pred.get("pct", 0))
            
            # Try to infer teams from active hits
            likely_favorite_team = None
            likely_underdog_team = None
            
            if active_hits:
                # Collect all teams mentioned in active hits
                teams_in_hits = {}  # team_id/name -> score
                for hit in active_hits:
                    team_name = str(hit.get("team", "")).strip()
                    team_id = str(hit.get("team_id", "")).strip()
                    score = _to_int_or_none(hit.get("score"))
                    
                    key = team_id or team_name.lower()
                    if key and key not in teams_in_hits:
                        teams_in_hits[key] = {"name": team_name, "id": team_id, "score": score}
                
                # If we have exactly 2 teams, sort by score (higher score = likely favorite)
                if len(teams_in_hits) == 2:
                    teams_list = list(teams_in_hits.values())
                    teams_list.sort(key=lambda t: (_to_int_or_none(t.get("score")) or -999), reverse=True)
                    likely_favorite_team = teams_list[0].get("name") or teams_list[0].get("id")
                    likely_underdog_team = teams_list[1].get("name") or teams_list[1].get("id")
            
            # If upset_pct > 50%, predict the underdog; else predict the favorite
            if upset_pct > 50 and likely_underdog_team:
                predicted_winner = likely_underdog_team
            elif upset_pct <= 50 and likely_favorite_team:
                predicted_winner = likely_favorite_team
            else:
                predicted_winner = None
            
            if predicted_winner:
                # Add inferred team wins prediction
                inferred_tw_pct = 100.0 - upset_pct if upset_pct > 50 else upset_pct
                tw_payload = {
                    "label": "Team wins (inferred from upset)",
                    "pct": round(inferred_tw_pct, 1),
                    "sample": upset_pred.get("sample", 0),
                    "effective_sample": upset_pred.get("effective_sample", 0.0),
                    "basis": "single",
                    "conditions": upset_pred.get("conditions", []),
                    "predicted_team": predicted_winner,
                }
                predictions.append(tw_payload)
                # Re-sort to keep highest prob at top
                predictions.sort(key=lambda x: (-1 if x["basis"] == "combo" else 0, -x["pct"]))

    # Attach 2-signal consensus metadata to the selected Team wins payload.
    def _team_wins_score(row):
        ws = _to_float_or_none(row.get("winner_score_pct"))
        if ws is not None:
            return ws
        pct = _to_float_or_none(row.get("pct"))
        return pct if pct is not None else 0.0

    team_wins_rows = [
        row for row in predictions
        if "team wins" in str(row.get("label", "")).lower() and str(row.get("predicted_team") or "").strip()
    ]
    if team_wins_rows:
        historical_rows = [row for row in team_wins_rows if str(row.get("basis") or "") != "ml"]
        ml_rows = [row for row in team_wins_rows if str(row.get("basis") or "") == "ml"]

        historical_top = max(historical_rows, key=_team_wins_score) if historical_rows else None
        ml_top = max(ml_rows, key=_team_wins_score) if ml_rows else None

        if historical_top and ml_top:
            hist_team = str(historical_top.get("predicted_team") or "").strip().lower()
            ml_team = str(ml_top.get("predicted_team") or "").strip().lower()
            consensus = "strong" if hist_team and ml_team and hist_team == ml_team else "conflicted"
        elif historical_top:
            consensus = "historical_only"
        elif ml_top:
            consensus = "ml_only"
        else:
            consensus = None

        if consensus:
            selected_row = max(team_wins_rows, key=_team_wins_score)
            selected_row["consensus"] = consensus

            # Modifier: consensus strength (#195.6)
            _cs_mod = None
            if consensus == "strong":
                _cs_mod = _to_float_or_none(_cfg.get("pw_consensus_boost_strong"))
            elif consensus == "conflicted":
                _cs_mod = _to_float_or_none(_cfg.get("pw_consensus_penalty_conflicted"))
            if _cs_mod is not None and _cs_mod != 1.0:
                _cs_ws = _to_float_or_none(selected_row.get("winner_score_pct"))
                if _cs_ws is not None:
                    selected_row["winner_score_pct"] = max(0.0, min(100.0, round(_cs_ws * _cs_mod, 1)))

    # Game-level polarity from all active conditions
    _game_pol_neg = sum(1 for c in active_conditions if classify_polarity(c) == "neg")
    _game_pol_pos = sum(1 for c in active_conditions if classify_polarity(c) == "pos")

    return {
        "active_conditions": list(active_conditions),
        "active_conditions_detailed": active_conditions_detailed,
        "predictions":       _cap_predictions(predictions, max_non_ml=10, max_ml=5),
        "suppressed_predictions": _pw_suppressed_rows,
        "polarity_pos": _game_pol_pos,
        "polarity_neg": _game_pol_neg,
        "polarity_net": _game_pol_pos - _game_pol_neg,
        "ml_status":         ml_status,
    }


def write_live_analysis(games_analysis):
    """
    Write live analysis for all active games to live_analysis.json.
    games_analysis: { game_id: { game_name, analysis_result } }
    """
    tmp = LIVE_ANALYSIS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "games":   games_analysis,
        }, f, indent=2)
    os.replace(tmp, LIVE_ANALYSIS_FILE)


def compute_pw_roi_combos(records, season_segment="", season_year="",
                          sort_by="ml_roi_pct", limit=10, min_calls=10):
    """
    Compute all combinations of PW call filters and rank by sort metric (#186).

    Dimensions (no "All" — every combo is fully specific):
      role:           "favorite", "underdog"                    (2)
      margin:         "trailing", "leading"                     (2)
      call_selection: "first", "first_per_quarter", "last"      (3)
      quarter:        "Q1", "Q2", "Q3", "Q4", "OT"             (5)

    Total: 2*2*3*5 = 60 combos.

    Returns dict with 'combos' list sorted by sort_by, 'total_computed', 'elapsed_ms'.
    """
    import time as _time
    t0 = _time.time()

    roles = ["favorite", "underdog"]
    margins = ["trailing", "leading"]
    selections = ["first", "first_per_quarter", "last"]
    quarters = ["Q1", "Q2", "Q3", "Q4", "OT"]

    # Pre-filter records by segment/season once
    filtered = records
    if season_segment or season_year:
        filtered = _filter_records_by_segment(filtered, season_segment, season_year=season_year)

    valid_sorts = {"games_won_pct", "flat_roi_pct", "ml_roi_pct",
                   "spread_covered_pct", "spread_roi_pct", "live_line_roi_pct",
                   "bk_line_roi_pct", "bk_odds_roi_pct"}
    if sort_by not in valid_sorts:
        sort_by = "ml_roi_pct"

    combos = []
    for role in roles:
        for margin in margins:
            for sel in selections:
                for qtr in quarters:
                    try:
                        pw = compute_predicted_winner_accuracy(
                            filtered, spread_role=role, margin_at_fire=margin,
                            quarter=qtr, matrix=False, call_selection=sel)
                    except Exception:
                        continue
                    tc = pw.get("total_calls", 0)
                    if tc < min_calls:
                        continue
                    combos.append({
                        "role": role,
                        "margin": margin,
                        "call_selection": sel,
                        "quarter": qtr,
                        "total_calls": tc,
                        "correct": pw.get("correct", 0),
                        "accuracy_pct": pw.get("accuracy_pct"),
                        "total_games": pw.get("total_games", 0),
                        "games_won_pct": pw.get("games_won_pct"),
                        "flat_roi_pct": pw.get("flat_roi_pct"),
                        "ml_roi_pct": pw.get("ml_roi_pct"),
                        "spread_covered_pct": pw.get("spread_covered_pct"),
                        "spread_roi_pct": pw.get("spread_roi_pct"),
                        "live_line_roi_pct": pw.get("live_line_roi_pct"),
                        "bk_line_roi_pct": pw.get("bk_line_roi_pct"),
                        "bk_odds_roi_pct": pw.get("bk_odds_roi_pct"),
                    })

    # Sort descending by chosen metric (None values last)
    combos.sort(key=lambda c: (c.get(sort_by) is not None, c.get(sort_by) or 0), reverse=True)
    total_computed = len(combos)
    if limit:
        combos = combos[:limit]

    elapsed_ms = int((_time.time() - t0) * 1000)
    return {"combos": combos, "total_computed": total_computed, "elapsed_ms": elapsed_ms}


def load_live_analysis():
    try:
        with open(LIVE_ANALYSIS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"updated": None, "games": {}}
