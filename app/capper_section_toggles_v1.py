from __future__ import annotations

from app import dashboard_sh01_capper_v3 as base


dashboard = base.dashboard
html = dashboard.DASHBOARD_HTML

if "capperSectionTogglesV1" not in html:
    css = r'''
.capper-section-badge.capper-section-toggle{
 cursor:pointer!important;
 user-select:none;
 padding-right:22px!important;
 position:relative;
}
.capper-section-badge.capper-section-toggle::after{
 content:'▾';
 position:absolute;
 right:7px;
 top:50%;
 transform:translateY(-50%);
 font-weight:900;
}
.capper-section-badge.capper-section-toggle.capper-section-collapsed::after{content:'▸'}
.capper-section-badge.capper-section-toggle:focus{outline:2px dotted #000;outline-offset:2px}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
<script>
const capperSectionTogglesV1=true;
const capperSectionCollapsedState=(()=>{
 try{return JSON.parse(localStorage.getItem('capperSectionCollapsedV1')||'{}')||{}}catch(e){return {}}
})();
function capperSectionSave(){try{localStorage.setItem('capperSectionCollapsedV1',JSON.stringify(capperSectionCollapsedState))}catch(e){}}
function capperSectionLabel(badge){return String(badge&&badge.textContent||'').replace(/[▾▸]/g,'').trim()}
function capperSectionKind(label){
 const low=String(label||'').toLowerCase();
 if(low.startsWith('open positions'))return 'open';
 if(low.startsWith('settled positions'))return 'settled';
 if(low==='signals'||low.startsWith('signals '))return 'signals';
 return '';
}
function capperSectionHostKey(badge){
 let node=badge.parentElement;
 while(node){if(node.id)return node.id;node=node.parentElement}
 const card=badge.closest('.nfl-capper-card,.cfb-capper-card,.monitor-card,.sh01-capper-card,.capper-card');
 if(card){
  const h=card.querySelector('h1,h2,h3,h4,.capper-card-title,.monitor-title');
  if(h)return String(h.textContent||'').trim().replace(/\s+/g,'_');
 }
 return 'capper';
}
function capperSectionKey(badge){return capperSectionHostKey(badge)+'::'+capperSectionKind(capperSectionLabel(badge))}
function capperSectionApply(badge,collapsed){
 const parent=badge.parentElement;if(!parent)return;
 badge.classList.toggle('capper-section-collapsed',!!collapsed);
 badge.setAttribute('aria-expanded',collapsed?'false':'true');
 Array.from(parent.children).forEach(child=>{if(child!==badge)child.style.display=collapsed?'none':''});
}
function capperSectionToggle(badge){
 const key=capperSectionKey(badge);
 const next=!badge.classList.contains('capper-section-collapsed');
 capperSectionCollapsedState[key]=next;
 capperSectionSave();
 capperSectionApply(badge,next);
}
function capperSectionUpgrade(root){
 const scope=root&&root.querySelectorAll?root:document;
 scope.querySelectorAll('.capper-section-badge').forEach(badge=>{
  const kind=capperSectionKind(capperSectionLabel(badge));
  if(!kind||badge.dataset.sectionToggleInstalled==='1')return;
  badge.dataset.sectionToggleInstalled='1';
  badge.classList.add('capper-section-toggle');
  badge.setAttribute('role','button');
  badge.setAttribute('tabindex','0');
  const key=capperSectionKey(badge);
  capperSectionApply(badge,!!capperSectionCollapsedState[key]);
  badge.addEventListener('click',()=>capperSectionToggle(badge));
  badge.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();capperSectionToggle(badge)}});
 });
}
function capperEnsureSettledSection(html,emptyText){
 const text=String(html||'');
 if(/Settled positions/i.test(text))return text;
 return text+'<div style="margin-top:12px"><span class="capper-section-badge">Settled positions</span><div style="margin-top:7px;opacity:.7">'+String(emptyText||'No settled positions.')+'</div></div>';
}

// NFL previously let the global "hide finished" switch remove the whole settled
// section. Positions and signals now have their own independent collapse controls,
// so always render the NFL settled section and let its button hide/show it.
if(typeof nflPositionList==='function'){
 const _nflPositionListSectionToggle=nflPositionList;
 nflPositionList=function(x){
  let previous=false,hadBinding=false;
  try{previous=nflHideFinished;hadBinding=true;nflHideFinished=false}catch(e){}
  let out;
  try{out=_nflPositionListSectionToggle(x)}finally{if(hadBinding){try{nflHideFinished=previous}catch(e){}}}
  return capperEnsureSettledSection(out,'No settled positions.');
 };
}
if(typeof cfbPositionList==='function'){
 const _cfbPositionListSectionToggle=cfbPositionList;
 cfbPositionList=function(x){return capperEnsureSettledSection(_cfbPositionListSectionToggle(x),'No settled positions.')} ;
}
if(typeof monitorPositions==='function'){
 const _monitorPositionsSectionToggle=monitorPositions;
 monitorPositions=function(x){return capperEnsureSettledSection(_monitorPositionsSectionToggle(x),'No settled positions.')} ;
}
if(typeof sh01PositionRows==='function'){
 const _sh01PositionRowsSectionToggle=sh01PositionRows;
 sh01PositionRows=function(x){return capperEnsureSettledSection(_sh01PositionRowsSectionToggle(x),'No settled positions.')} ;
}

capperSectionUpgrade(document);
const capperSectionObserver=new MutationObserver(mutations=>{
 mutations.forEach(m=>m.addedNodes.forEach(node=>{if(node&&node.nodeType===1)capperSectionUpgrade(node)}));
});
capperSectionObserver.observe(document.body,{childList:true,subtree:true});
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
