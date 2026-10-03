from __future__ import annotations

from app import ufc_live_betting_splits_v1 as splits

# CLEATZ currently renders UFC fighter rows as compact moneyline rows such as
# "Court McGee +190 +41 24% 65%" and keeps the Bets/Handle labels in the table
# footer.  The original parser expected literal "Bets ... Handle ..." labels
# after every fighter, which caused otherwise valid full-card rows to disappear.
# Keep the labelled format for backwards compatibility and add the compact row
# format used by the live page.  Iterate fighter-name occurrences backwards so
# the actual moneyline row wins over the matchup heading; require an American
# odds token for the compact fallback so "Moves since open" percentages cannot
# be mistaken for current ticket/handle splits.
splits._NAME_ALIASES["Wang Cong"] = ["Wang Cong", "Cong Wang"]


def _fighter_row_full_card(window: str, name: str):
    for alias in splits._aliases(name):
        occurrences = list(splits.re.finditer(splits.re.escape(alias), window, flags=splits.re.I))
        for occurrence in reversed(occurrences):
            tail = window[occurrence.end() : occurrence.end() + 180]

            labelled = splits.re.search(
                r"(?P<between>.{0,110}?)\bBets\s*(?P<bets>\d{1,3})%\s*Handle\s*(?P<handle>\d{1,3})%",
                tail,
                flags=splits.re.I,
            )
            if labelled:
                bets = int(labelled.group("bets"))
                handle = int(labelled.group("handle"))
                if 0 <= bets <= 100 and 0 <= handle <= 100:
                    between = labelled.group("between") or ""
                    odds_match = splits.re.search(r"(?<!\d)([+-]\d{2,4})(?!\d)", between)
                    return {
                        "bets_pct": f"{bets}%",
                        "handle_pct": f"{handle}%",
                        "odds": odds_match.group(1) if odds_match else None,
                    }

            percentages = list(splits.re.finditer(r"(?<!\d)(\d{1,3})%", tail))
            if len(percentages) < 2:
                continue
            bets = int(percentages[0].group(1))
            handle = int(percentages[1].group(1))
            if not (0 <= bets <= 100 and 0 <= handle <= 100):
                continue

            before_bets = tail[: percentages[0].start()]
            odds_match = splits.re.search(r"(?<!\d)([+-]\d{2,4})(?!\d)", before_bets)
            if not odds_match:
                continue
            return {
                "bets_pct": f"{bets}%",
                "handle_pct": f"{handle}%",
                "odds": odds_match.group(1),
            }
    return None


splits._fighter_row = _fighter_row_full_card

live = splits.live
html = live.UFC_LIVE_HTML
marker = "ufc-splits-main-row-fix-v1"
if marker not in html:
    patch = r'''
<style id="ufc-splits-main-row-fix-v1">#ufcSplitLive{display:none!important}.fight .splits .sl{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid #444;padding-top:4px;margin-top:4px}.fight .splits .ss{color:#aaa;font-size:9px;margin-top:4px}.fight .splits .sharp{color:#00d060;font-weight:900}.fight .splits .public{color:#ffd34d;font-weight:900}</style>
<script>
(function(){
 let busy=false,t=null,last='';
 const pct=v=>{const n=Number(String(v??'').replace('%',''));return Number.isFinite(n)?n:null};
 function show(d){const e=document.querySelector('#fightHost .fight .splits');if(!e)return;const s=d?.betting_splits;if(!d?.available||!s?.fighters){e.innerHTML='<b>BETTING SPLITS</b> · no current split available';return}const f=typeof current==='function'?current():null;let h='<b>BETTING SPLITS</b>';for(const n of [f?.fighter_a,f?.fighter_b].filter(Boolean)){const x=s.fighters?.[n];if(!x)continue;const b=pct(x.bets_pct),q=pct(x.handle_pct),g=b!==null&&q!==null?q-b:null;let c='',tag='';if(g!==null&&g>=8){c='sharp';tag=' · SHARP +'+g.toFixed(0)+'pp'}else if(g!==null&&g<=-8){c='public';tag=' · PUBLIC '+g.toFixed(0)+'pp'}h+='<div class="sl"><span>'+esc(n)+(x.odds?' · '+esc(x.odds):'')+'</span><span class="'+c+'">Bets '+esc(x.bets_pct||'—')+' · Handle '+esc(x.handle_pct||'—')+tag+'</span></div>'}h+='<div class="ss">'+esc(s.source||'Audit DB')+(s.observed_at?' · '+esc(s.observed_at):'')+(s.fallback?' · SNAPSHOT':' · LIVE')+'</div>';e.innerHTML=h}
 async function run(){clearTimeout(t);if(busy||document.hidden){t=setTimeout(run,5000);return}const f=typeof current==='function'?current():null,id=String(f?.fight_id||'');if(!id){t=setTimeout(run,2500);return}busy=true;last=id;try{const r=await fetch('/api/ufc-live/betting-splits?fight_id='+encodeURIComponent(id),{cache:'no-store'}),d=await r.json();if(r.ok&&id===last)show(d)}catch(_e){}finally{busy=false;t=setTimeout(run,5000)}}
 const oc=window.choose;if(typeof oc==='function')window.choose=function(id){const x=oc(id);setTimeout(run,50);return x};const ot=window.setTab;if(typeof ot==='function')window.setTab=function(x){const y=ot(x);setTimeout(run,50);return y};setTimeout(run,150);
})();
</script>
'''
    html = html.replace('</body>', patch + '</body>', 1)
    live.UFC_LIVE_HTML = html
