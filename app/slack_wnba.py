from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app import slack_ingest as ingest

app = ingest.app


def _clean(value: str) -> str:
    return re.sub(r"[*_`]", "", value or "").strip()


def _canonical_team(value: str) -> str | None:
    target = ingest._norm_text(_clean(value))
    for team, aliases in ingest.WNBA_ALIASES.items():
        if target == ingest._norm_text(team):
            return team
        if any(target == ingest._norm_text(alias) for alias in aliases):
            return team
    # Header normally contains a full team name. Fall back to contained aliases.
    for team, aliases in ingest.WNBA_ALIASES.items():
        if ingest._norm_text(team) in target:
            return team
        if any(len(alias) > 3 and ingest._norm_text(alias) in target for alias in aliases):
            return team
    return None


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"(?:^|[·\n])\s*{re.escape(name)}\s*:\s*([^·\n]+)", text, flags=re.I)
    return _clean(m.group(1)) if m else None


def _parse_alert(text: str) -> dict[str, Any]:
    raw = text or ""
    cleaned = _clean(raw)
    teams = ingest._team_mentions(raw)

    header = re.search(r"Predicted\s+Winner\s*[—-]\s*([^\n]+)", cleaned, flags=re.I)
    alert_type = "predicted_winner" if header else "other"
    selection = _canonical_team(header.group(1)) if header else None

    # Explicitly ignore non-bet channel posts such as Final / GAME SUMMARY.
    if not header:
        return {
            "raw_text": raw,
            "teams": teams,
            "market_kind": "moneyline",
            "selection": None,
            "line": None,
            "units": None,
            "alert_type": alert_type,
            "actionable": False,
            "ignore_reason": "Not a Predicted Winner alert",
        }

    prob_match = re.search(r"(\d{1,3})%\s+win\s+probability", cleaned, flags=re.I)
    consensus_match = re.search(r"consensus\s*:\s*([^\n·]+)", cleaned, flags=re.I)
    game_match = re.search(r"Game\s*:\s*([^\n·]+?)\s+vs\s+([^\n·]+?)\s*[·\n]", cleaned, flags=re.I)
    quarter_match = re.search(r"Game\s*:[^\n]+?[·]\s*(Q[1-4]|OT\d*)", cleaned, flags=re.I)
    edge_match = re.search(r"Edge\s*:\s*([+\-]?\d+(?:\.\d+)?)", cleaned, flags=re.I)
    pol_match = re.search(r"Pol\s*:\s*([^·\n]+)", cleaned, flags=re.I)

    win_probability = int(prob_match.group(1)) if prob_match else None
    consensus = _clean(consensus_match.group(1)) if consensus_match else None
    game_teams = []
    if game_match:
        for raw_team in (game_match.group(1), game_match.group(2)):
            team = _canonical_team(raw_team)
            if team and team not in game_teams:
                game_teams.append(team)
    if selection and selection not in game_teams:
        game_teams.insert(0, selection)

    parsed = {
        "raw_text": raw,
        "teams": game_teams or teams,
        "market_kind": "moneyline",
        "selection": selection,
        "line": None,
        "units": None,
        "alert_type": alert_type,
        "actionable": bool(selection),
        "win_probability": win_probability,
        "consensus": consensus,
        "quarter": quarter_match.group(1).upper() if quarter_match else None,
        "score": _field(cleaned, "Score"),
        "margin": _field(cleaned, "Margin"),
        "pregame_odds": _field(cleaned, "Odds"),
        "pregame_spread": _field(cleaned, "Spread"),
        "live_ml": _field(cleaned, "Live ML"),
        "handicap": _field(cleaned, "Handicap"),
        "live_spread": _field(cleaned, "Live Spread"),
        "bk_odds": _field(cleaned, "BK Odds"),
        "bk_spread": _field(cleaned, "BK Spread"),
        "pol": _clean(pol_match.group(1)) if pol_match else None,
        "edge": edge_match.group(1) if edge_match else None,
        "scenario": _field(cleaned, "Scenario"),
    }
    return parsed


_original_paper_trade = ingest._paper_trade_from_alert


def _paper_trade_from_alert(parsed: dict[str, Any], slack_event_id: str, slack_event: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("alert_type") != "predicted_winner" or not parsed.get("actionable"):
        raise ValueError(parsed.get("ignore_reason") or "Slack alert is not an actionable Predicted Winner signal")
    if parsed.get("market_kind") != "moneyline":
        raise ValueError("WNBA Slack automation currently paper-trades moneyline only")
    if not parsed.get("selection"):
        raise ValueError("Could not identify predicted winner")
    return _original_paper_trade(parsed, slack_event_id, slack_event)


# Patch the already-registered Slack endpoint's runtime globals.
ingest._parse_alert = _parse_alert
ingest._paper_trade_from_alert = _paper_trade_from_alert


@app.get("/api/slack/wnba-parser")
def wnba_parser_status():
    return {
        "ok": True,
        "mode": "paper_only",
        "channel_id": ingest.SLACK_CHANNEL_ID or None,
        "signal_type": "Predicted Winner",
        "market_kind": "moneyline",
        "events_enabled": ingest.SLACK_EVENTS_ENABLED,
        "signing_secret_configured": bool(ingest.SLACK_SIGNING_SECRET),
        "paper_budget_usdc": str(ingest.SLACK_PAPER_BUDGET_USDC),
    }
