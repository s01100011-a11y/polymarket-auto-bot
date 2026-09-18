from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from fastapi import Depends, Query
from fastapi.responses import FileResponse, StreamingResponse

from app import dashboard_pnl_filters_v6 as base

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.base.ingest

DB_PATH = core.DATA_DIR / "wnba_pw_alerts.sqlite"
BACKFILL_DIR = Path(__file__).resolve().parent.parent / "data"
TEAM_ABBR = {
    "Atlanta Dream": "ATL",
    "Chicago Sky": "CHI",
    "Connecticut Sun": "CON",
    "Dallas Wings": "DAL",
    "Golden State Valkyries": "GS",
    "Indiana Fever": "IND",
    "Las Vegas Aces": "LV",
    "Los Angeles Sparks": "LA",
    "Minnesota Lynx": "MIN",
    "New York Liberty": "NY",
    "Phoenix Mercury": "PHX",
    "Portland Fire": "POR",
    "Seattle Storm": "SEA",
    "Toronto Tempo": "TOR",
    "Washington Mystics": "WSH",
}


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _init_schema() -> None:
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                final_ts TEXT,
                team_a TEXT,
                score_a INTEGER,
                team_b TEXT,
                score_b INTEGER,
                winner_abbr TEXT,
                source_channel TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                source_channel TEXT NOT NULL,
                source_page INTEGER,
                source_index INTEGER,
                event_ts TEXT,
                game_id TEXT,
                predicted_winner TEXT,
                predicted_winner_abbr TEXT,
                quarter TEXT,
                win_probability REAL,
                consensus TEXT,
                bk_ml INTEGER,
                score_at_alert TEXT,
                margin INTEGER,
                pregame_odds INTEGER,
                handicap REAL,
                live_spread REAL,
                bk_spread REAL,
                edge REAL,
                alert_format TEXT,
                is_test INTEGER NOT NULL DEFAULT 0,
                result TEXT,
                stake_usdc REAL,
                profit_usdc REAL,
                return_usdc REAL,
                backtest_eligible INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pw_game ON alerts(game_id);
            CREATE INDEX IF NOT EXISTS idx_pw_ts ON alerts(event_ts);
            CREATE INDEX IF NOT EXISTS idx_pw_quarter ON alerts(quarter);
            CREATE INDEX IF NOT EXISTS idx_pw_team ON alerts(predicted_winner);
            CREATE INDEX IF NOT EXISTS idx_pw_bk ON alerts(bk_ml);
            """
        )


def _american_profit(odds: int, stake: float = 100.0) -> float:
    if odds > 0:
        return stake * odds / 100.0
    return stake * 100.0 / abs(odds)


def _source_key(row: dict[str, Any], fallback: str = "") -> str:
    if row.get("event_id"):
        return f"event:{row['event_id']}"
    return "|".join(
        [
            str(row.get("source_channel") or ""),
            str(row.get("source_page") if row.get("source_page") is not None else ""),
            str(row.get("source_index") if row.get("source_index") is not None else ""),
            str(row.get("game_id") or ""),
            str(row.get("event_ts") or ""),
            str(row.get("predicted_winner") or fallback),
            str(row.get("bk_ml") if row.get("bk_ml") is not None else ""),
        ]
    )


def _upsert_final(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    if not row.get("game_id") or not row.get("winner_abbr"):
        return
    con.execute(
        """
        INSERT INTO games(game_id, final_ts, team_a, score_a, team_b, score_b, winner_abbr, source_channel)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(game_id) DO UPDATE SET
            final_ts=excluded.final_ts,
            team_a=COALESCE(excluded.team_a,games.team_a),
            score_a=COALESCE(excluded.score_a,games.score_a),
            team_b=COALESCE(excluded.team_b,games.team_b),
            score_b=COALESCE(excluded.score_b,games.score_b),
            winner_abbr=excluded.winner_abbr,
            source_channel=excluded.source_channel
        """,
        (
            row.get("game_id"),
            row.get("event_ts"),
            row.get("team_a"),
            row.get("score_a"),
            row.get("team_b"),
            row.get("score_b"),
            row.get("winner_abbr"),
            row.get("source_channel"),
        ),
    )


def _insert_alert(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    pick = row.get("predicted_winner")
    if pick not in TEAM_ABBR:
        return
    key = _source_key(row, pick)
    con.execute(
        """
        INSERT OR IGNORE INTO alerts(
            source_key,source_channel,source_page,source_index,event_ts,game_id,
            predicted_winner,predicted_winner_abbr,quarter,win_probability,consensus,bk_ml,
            score_at_alert,margin,pregame_odds,handicap,live_spread,bk_spread,edge,
            alert_format,is_test,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            key,
            row.get("source_channel") or "unknown",
            row.get("source_page"),
            row.get("source_index"),
            row.get("event_ts"),
            row.get("game_id"),
            pick,
            TEAM_ABBR.get(pick),
            row.get("quarter"),
            row.get("win_probability"),
            row.get("consensus"),
            row.get("bk_ml"),
            row.get("score_at_alert"),
            row.get("margin"),
            row.get("pregame_odds"),
            row.get("handicap"),
            row.get("live_spread"),
            row.get("bk_spread"),
            row.get("edge"),
            row.get("alert_format"),
            1 if row.get("is_test") else 0,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _grade_all(con: sqlite3.Connection) -> None:
    rows = con.execute(
        """
        SELECT a.id,a.predicted_winner_abbr,a.bk_ml,a.is_test,g.winner_abbr
        FROM alerts a LEFT JOIN games g ON g.game_id=a.game_id
        """
    ).fetchall()
    for row in rows:
        eligible = bool(
            row["winner_abbr"]
            and row["predicted_winner_abbr"]
            and row["bk_ml"] is not None
            and not row["is_test"]
        )
        if not eligible:
            con.execute(
                "UPDATE alerts SET result=NULL,stake_usdc=NULL,profit_usdc=NULL,return_usdc=NULL,backtest_eligible=0 WHERE id=?",
                (row["id"],),
            )
            continue
        won = row["predicted_winner_abbr"] == row["winner_abbr"]
        stake = 100.0
        profit = _american_profit(int(row["bk_ml"]), stake) if won else -stake
        con.execute(
            """
            UPDATE alerts
            SET result=?,stake_usdc=?,profit_usdc=?,return_usdc=?,backtest_eligible=1
            WHERE id=?
            """,
            ("W" if won else "L", stake, profit, stake + profit, row["id"]),
        )


def _load_backfill() -> None:
    with _db() as con:
        for path in sorted(BACKFILL_DIR.glob("wnba_pw_backfill_*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("record_type") == "final":
                    _upsert_final(con, row)
                elif row.get("record_type") == "alert":
                    _insert_alert(con, row)
        _grade_all(con)


def _parse_current_message(text: str, event_id: str | None = None) -> list[dict[str, Any]]:
    game_id = None
    m = re.search(r"gameId/(\d+)", text, re.I)
    if m:
        game_id = m.group(1)
    if not game_id:
        return []

    ts_match = list(re.finditer(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+08)", text))
    event_ts = ts_match[-1].group(1) if ts_match else None

    final = re.search(r"\*Final\*\s+—\s+([A-Z]{2,3})\s+(\d+)\s+·\s+([A-Z]{2,3})\s+(\d+)", text)
    winner = re.search(r":trophy:\s+([A-Z]{2,3}) win", text)
    if final and winner:
        return [{
            "record_type": "final",
            "source_channel": "wnba-alerts",
            "event_ts": event_ts,
            "game_id": game_id,
            "team_a": final.group(1),
            "score_a": int(final.group(2)),
            "team_b": final.group(3),
            "score_b": int(final.group(4)),
            "winner_abbr": winner.group(1),
            "event_id": event_id,
        }]

    if "Predicted Winner" not in text:
        return []
    pick_match = re.search(r"Predicted Winner:\s*\*([^*]+)\*", text, re.I) or re.search(
        r"\*Predicted Winner\*\s+—\s+([^\n]+)", text, re.I
    )
    pick = pick_match.group(1).strip() if pick_match else None
    if pick not in TEAM_ABBR:
        return []

    def num(pattern: str) -> float | int | None:
        mm = re.search(pattern, text, re.I)
        if not mm:
            return None
        raw = mm.group(1)
        try:
            return float(raw) if "." in raw else int(raw)
        except Exception:
            return None

    q = re.search(r"(?:^|\n)(Q[1-4])\b", text, re.I) or re.search(r"·\s*(Q[1-4])\b", text, re.I)
    consensus = re.search(r"consensus:\s*([^·\n]+)", text, re.I)
    bk = re.search(r"\bBK (?:ML|Odds):\s*([+-]\d+)", text, re.I)
    prob = re.search(r"(\d+(?:\.\d+)?)%\s*win probability", text, re.I) or re.search(
        r"Predicted Winner:\s*\*[^*]+\*\s*\((\d+(?:\.\d+)?)%\)", text, re.I
    )
    score = re.search(r"Score:\s*([0-9]+-[0-9]+)", text, re.I)
    return [{
        "record_type": "alert",
        "source_channel": "wnba-alerts",
        "event_ts": event_ts,
        "game_id": game_id,
        "predicted_winner": pick,
        "quarter": q.group(1).upper() if q else None,
        "win_probability": float(prob.group(1)) if prob else None,
        "consensus": consensus.group(1).strip().lower() if consensus else None,
        "bk_ml": int(bk.group(1)) if bk else None,
        "score_at_alert": score.group(1) if score else None,
        "margin": num(r"Margin:\s*([+-]?\d+)"),
        "pregame_odds": num(r"(?:·|\n)\s*Odds:\s*([+-]\d+)"),
        "handicap": num(r"(?:Handicap|Spread):\s*([+-]?\d+(?:\.\d+)?)"),
        "live_spread": num(r"Live Spread:\s*([+-]?\d+(?:\.\d+)?)"),
        "bk_spread": num(r"BK Spread:\s*([+-]?\d+(?:\.\d+)?)"),
        "edge": num(r"Edge:\s*([+-]?\d+(?:\.\d+)?)"),
        "is_test": "This is a test alert" in text,
        "alert_format": "wnba_pw_alert" if "WNBA PW Alert" in text else "predicted_winner",
        "event_id": event_id,
    }]


def _ingest_saved_alerts() -> None:
    try:
        saved = core._load(ingest.SLACK_ALERTS_FILE)
    except Exception:
        saved = {}
    with _db() as con:
        for event_id, rec in saved.items():
            text = str((rec or {}).get("text") or "")
            for row in _parse_current_message(text, str(event_id)):
                if row.get("record_type") == "final":
                    _upsert_final(con, row)
                else:
                    _insert_alert(con, row)
        _grade_all(con)


_ORIGINAL_SAVE_ALERT = ingest._save_alert


def _save_alert_and_history(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    saved = _ORIGINAL_SAVE_ALERT(event_id, payload)
    text = str((payload or {}).get("text") or "")
    if text:
        with _db() as con:
            for row in _parse_current_message(text, event_id):
                if row.get("record_type") == "final":
                    _upsert_final(con, row)
                else:
                    _insert_alert(con, row)
            _grade_all(con)
    return saved


ingest._save_alert = _save_alert_and_history


def _filters(
    quarter: str | None,
    team: str | None,
    consensus: str | None,
    min_prob: float | None,
    max_prob: float | None,
    min_bk: int | None,
    max_bk: int | None,
    source: str | None,
) -> tuple[str, list[Any]]:
    where = ["1=1"]
    args: list[Any] = []
    if quarter:
        where.append("quarter=?")
        args.append(quarter.upper())
    if team:
        where.append("predicted_winner=?")
        args.append(team)
    if consensus:
        where.append("consensus=?")
        args.append(consensus.lower())
    if min_prob is not None:
        where.append("win_probability>=?")
        args.append(min_prob)
    if max_prob is not None:
        where.append("win_probability<=?")
        args.append(max_prob)
    if min_bk is not None:
        where.append("bk_ml>=?")
        args.append(min_bk)
    if max_bk is not None:
        where.append("bk_ml<=?")
        args.append(max_bk)
    if source:
        where.append("source_channel=?")
        args.append(source)
    return " AND ".join(where), args


@app.get("/api/wnba-pw/summary", dependencies=[Depends(dashboard._auth)])
def wnba_pw_summary(
    quarter: str | None = None,
    team: str | None = None,
    consensus: str | None = None,
    min_prob: float | None = None,
    max_prob: float | None = None,
    min_bk: int | None = None,
    max_bk: int | None = None,
    source: str | None = None,
):
    where, args = _filters(quarter, team, consensus, min_prob, max_prob, min_bk, max_bk, source)
    with _db() as con:
        total = con.execute(f"SELECT COUNT(*) c FROM alerts WHERE {where}", args).fetchone()["c"]
        missing = con.execute(
            f"SELECT COUNT(*) c FROM alerts WHERE {where} AND bk_ml IS NULL", args
        ).fetchone()["c"]
        row = con.execute(
            f"""
            SELECT
                COUNT(*) graded,
                SUM(CASE WHEN result='W' THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN result='L' THEN 1 ELSE 0 END) losses,
                COALESCE(SUM(stake_usdc),0) total_staked,
                COALESCE(SUM(profit_usdc),0) pnl
            FROM alerts
            WHERE {where} AND backtest_eligible=1
            """,
            args,
        ).fetchone()
    graded = int(row["graded"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    staked = float(row["total_staked"] or 0)
    pnl = float(row["pnl"] or 0)
    return {
        "alerts": total,
        "graded": graded,
        "missing_bk_ml": missing,
        "wins": wins,
        "losses": losses,
        "accuracy_pct": round(wins / (wins + losses) * 100, 2) if wins + losses else None,
        "total_staked_usdc": round(staked, 2),
        "pnl_usdc": round(pnl, 2),
        "gross_return_usdc": round(staked + pnl, 2),
        "roi_pct": round(pnl / staked * 100, 2) if staked else None,
    }


@app.get("/api/wnba-pw/alerts", dependencies=[Depends(dashboard._auth)])
def wnba_pw_alerts(
    quarter: str | None = None,
    team: str | None = None,
    consensus: str | None = None,
    min_prob: float | None = None,
    max_prob: float | None = None,
    min_bk: int | None = None,
    max_bk: int | None = None,
    source: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    where, args = _filters(quarter, team, consensus, min_prob, max_prob, min_bk, max_bk, source)
    with _db() as con:
        rows = con.execute(
            f"""
            SELECT a.*,g.winner_abbr,g.team_a,g.score_a,g.team_b,g.score_b
            FROM alerts a LEFT JOIN games g ON g.game_id=a.game_id
            WHERE {where}
            ORDER BY event_ts DESC,id DESC
            LIMIT ? OFFSET ?
            """,
            [*args, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows]


def _xlsx_xml(rows: list[dict[str, Any]], summary: dict[str, Any]) -> bytes:
    columns = [
        "event_ts","source_channel","game_id","predicted_winner","quarter","win_probability",
        "consensus","bk_ml","score_at_alert","margin","pregame_odds","handicap","live_spread",
        "bk_spread","edge","winner_abbr","result","stake_usdc","profit_usdc","return_usdc",
        "backtest_eligible","is_test"
    ]

    def col_letter(n: int) -> str:
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    def cell(ref: str, value: Any, style: int = 0) -> str:
        if value is None:
            return f'<c r="{ref}" s="{style}"/>'
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b" s="{style}"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t>{escape(str(value))}</t></is></c>'

    alert_rows = []
    header = "".join(cell(f"{col_letter(i+1)}1", name, 1) for i, name in enumerate(columns))
    alert_rows.append(f'<row r="1">{header}</row>')
    for r_idx, row in enumerate(rows, start=2):
        cells = "".join(
            cell(f"{col_letter(c_idx+1)}{r_idx}", row.get(name), 0)
            for c_idx, name in enumerate(columns)
        )
        alert_rows.append(f'<row r="{r_idx}">{cells}</row>')
    end_col = col_letter(len(columns))
    sheet1 = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{"".join(f'<col min="{i}" max="{i}" width="{24 if i in (1,4,7) else 14}" customWidth="1"/>' for i in range(1,len(columns)+1))}</cols>
  <sheetData>{"".join(alert_rows)}</sheetData>
  <autoFilter ref="A1:{end_col}{max(1,len(rows)+1)}"/>
</worksheet>'''

    summary_items = [
        ("Metric","Value"),
        ("Alerts",summary.get("alerts")),
        ("Graded",summary.get("graded")),
        ("Wins",summary.get("wins")),
        ("Losses",summary.get("losses")),
        ("Accuracy %",summary.get("accuracy_pct")),
        ("Total staked USDC",summary.get("total_staked_usdc")),
        ("Net P/L USDC",summary.get("pnl_usdc")),
        ("Gross return USDC",summary.get("gross_return_usdc")),
        ("ROI %",summary.get("roi_pct")),
        ("Missing BK ML/Odds",summary.get("missing_bk_ml")),
        ("Assumption","$100 staked on every eligible PW alert at posted BK ML/BK Odds"),
    ]
    sr = []
    for i, (k, v) in enumerate(summary_items, start=1):
        sr.append(f'<row r="{i}">{cell(f"A{i}",k,1 if i==1 else 0)}{cell(f"B{i}",v,1 if i==1 else 0)}</row>')
    sheet2 = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <cols><col min="1" max="1" width="28" customWidth="1"/><col min="2" max="2" width="46" customWidth="1"/></cols>
  <sheetData>{"".join(sr)}</sheetData>
</worksheet>'''

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Alerts" sheetId="1" r:id="rId1"/><sheet name="Summary" sheetId="2" r:id="rId2"/></sheets>
</workbook>'''
    wb_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="10"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Arial"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/></cellXfs>
</styleSheet>'''

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)
    return out.getvalue()


@app.get("/api/wnba-pw/export.xlsx", dependencies=[Depends(dashboard._auth)])
def wnba_pw_export_xlsx():
    with _db() as con:
        rows = [
            dict(r)
            for r in con.execute(
                """
                SELECT a.*,g.winner_abbr
                FROM alerts a LEFT JOIN games g ON g.game_id=a.game_id
                ORDER BY event_ts ASC,id ASC
                """
            ).fetchall()
        ]
    summary = wnba_pw_summary()
    payload = _xlsx_xml(rows, summary)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="wnba_pw_alerts.xlsx"'},
    )


@app.get("/api/wnba-pw/database.sqlite", dependencies=[Depends(dashboard._auth)])
def wnba_pw_database():
    return FileResponse(DB_PATH, media_type="application/vnd.sqlite3", filename="wnba_pw_alerts.sqlite")


def _install_history_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="wnbaHistoryPanel"' in html:
        return
    box = """
    <div class="slack-mode-box" id="wnbaHistoryPanel" style="margin-top:16px">
      <div class="slack-mode-head">
        <div>
          <div class="label">WNBA PW historical database</div>
          <div class="slack-mode-value" id="wnbaHistHeadline">Loading…</div>
        </div>
        <div class="slack-mode-state" id="wnbaHistSub">Historical flat-$100 backtest</div>
      </div>
      <div class="slack-mode-controls">
        <a class="mode-paper-btn" href="/api/wnba-pw/export.xlsx">DOWNLOAD XLSX</a>
        <a class="mode-paper-btn" href="/api/wnba-pw/database.sqlite">DOWNLOAD SQLITE</a>
      </div>
      <div class="slack-mode-note">One row per WNBA PW alert. Uses posted BK ML/BK Odds, $100 flat stake, and final game result. Future WNBA alerts/finals are appended automatically.</div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', box + '<form id="settingsForm">', 1)
    js = r"""
async function loadWnbaHistorySummary(){
 try{
  const r=await fetch('/api/wnba-pw/summary',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'history summary failed');
  const h=document.getElementById('wnbaHistHeadline'),s=document.getElementById('wnbaHistSub');
  if(h)h.textContent=(d.wins||0)+'-'+(d.losses||0)+' · '+(d.roi_pct==null?'—':Number(d.roi_pct).toFixed(2)+'% ROI');
  if(s)s.textContent=(d.graded||0)+' graded · $'+Number(d.total_staked_usdc||0).toFixed(0)+' staked · P/L $'+Number(d.pnl_usdc||0).toFixed(2)+' · '+(d.missing_bk_ml||0)+' missing BK ML';
 }catch(e){}
}
loadWnbaHistorySummary();
setInterval(loadWnbaHistorySummary,30000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_init_schema()
_load_backfill()
_ingest_saved_alerts()
_install_history_ui()
