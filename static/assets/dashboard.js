// Dashboard JS — extrait de index.html pour cache long terme (P1.6).
// Voir /static/index.html pour l'historique. Modifier ce fichier directement.

// ─── Trusted Types : default policy pass-through ────────────────────────────
// La CSP contient `require-trusted-types-for 'script'` (Best Practices
// Lighthouse). Le code existant utilise innerHTML à plusieurs endroits ;
// pour ne pas casser, on installe une policy "default" qui accepte tout
// string et le retourne tel quel comme TrustedHTML/TrustedScript.
//
// Ceci satisfait la CSP sans changer le code applicatif. La vraie protection
// contre le DOM-based XSS reste à faire (DOMPurify ou refactor en
// textContent / templates), mais c'est un chantier distinct.
//
// Doit s'exécuter le plus tôt possible (avant tout innerHTML).
if (window.trustedTypes && window.trustedTypes.createPolicy) {
  try {
    window.trustedTypes.createPolicy('default', {
      createHTML:      (input) => input,
      createScript:    (input) => input,
      createScriptURL: (input) => input,
    });
  } catch (e) {
    // policy déjà créée (re-load soft) — ignore
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Toast
// ═════════════════════════════════════════════════════════════════════════════
function showToast(msg, type='', duration=2600){
  const el=document.createElement('div');
  el.className='toast'+(type?' toast--'+type:'');
  el.textContent=msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(()=>el.remove(), duration+300);
}

// ═════════════════════════════════════════════════════════════════════════════
// Back-to-top
// ═════════════════════════════════════════════════════════════════════════════
window.addEventListener('scroll',()=>{
  document.getElementById('back-top')?.classList.toggle('is-visible', window.scrollY>350);
},{passive:true});

// ═════════════════════════════════════════════════════════════════════════════
// Config — tous les intervalles & seuils centralisés (lus aussi par app.js)
// ═════════════════════════════════════════════════════════════════════════════
const WW_CONFIG = Object.freeze({
  // Rafraîchissement données (ms)
  PRICE_REFRESH_MS:      30_000,        // CoinGecko ETH/BTC
  WALLETS_REFRESH_MS:    60_000,        // /api/wallets (light, JSON cache)
  PATTERNS_REFRESH_MS:   5 * 60_000,    // /api/patterns (heavy, Dune)
  STATUS_POLL_MS:        2_500,         // /api/status pendant analyse
  AUTO_REFRESH_MS:       5 * 60_000,    // Auto re-Sonar (full pipeline)
  // Polling patterns en attente
  PATTERNS_POLL_MS:      5_000,
  PATTERNS_POLL_MAX:     60,            // 60 × 5s = 5min
  // Fraîcheur Trading Zones (heures)
  FRESHNESS_OK_H:        6,             // < 6h  → vert
  FRESHNESS_WARN_H:      24,            // 6-24h → orange ; > 24h → stale rouge
});
window.WW_CONFIG = WW_CONFIG;

// ═════════════════════════════════════════════════════════════════════════════
// State
// ═════════════════════════════════════════════════════════════════════════════
let allWallets=[], filtered=[], maxVol=0;
let _whaleAlertShown = false;
let _mevBuyMedian = 0, _mevSellMedian = 0, _mevRefEth = 0, _analysisEthPrice = 0;
let catFilter='all', search='', sortField='total_volume_usd', sortAsc=false;
let page=1; const PER_PAGE=25;
let chartBar=null, chartPie=null, poll=null;
let _sorted=[];

// ═════════════════════════════════════════════════════════════════════════════
// Sentiment score
// ═════════════════════════════════════════════════════════════════════════════
function computeSentiment(wallets){
  if(!wallets.length) return null;
  const total = wallets.length;
  const mevCount = wallets.filter(w=>w.category==='MEV Bot').length;
  const mevRatio = mevCount / total;
  const totalVol = wallets.reduce((s,w)=>s+(w.total_volume_usd||0),0);
  const top5Vol  = wallets.slice(0,5).reduce((s,w)=>s+(w.total_volume_usd||0),0);
  const concRatio = totalVol > 0 ? top5Vol / totalVol : 0.5;
  let score = 50;
  score -= mevRatio * 90;
  score -= Math.max(0, concRatio - 0.2) * 55;
  return Math.max(5, Math.min(95, Math.round(score)));
}

function renderSentimentBadge(score){
  const badge = document.getElementById('sentiment-badge');
  if(!badge || score === null) return;
  let label, color;
  if(score < 25)      { label='Extrême Peur';  color='var(--c-mev)'; }
  else if(score < 45) { label='Peur';           color='#ff9663'; }
  else if(score < 55) { label='Neutre';         color='var(--c-sc)'; }
  else if(score < 75) { label='Optimisme';      color='var(--green)'; }
  else                { label='Euphorie';       color='var(--cyan)'; }
  badge.innerHTML = `<span class="sentiment-dot"></span>Sentiment · ${label} · ${score}`;
  badge.style.color = color;
  badge.style.background = `color-mix(in srgb, ${color} 12%, transparent)`;
  badge.style.border = `1px solid color-mix(in srgb, ${color} 30%, transparent)`;
  badge.classList.remove('hidden');
}

// ═════════════════════════════════════════════════════════════════════════════
// Pro mode — toggle (persisted in localStorage)
// ═════════════════════════════════════════════════════════════════════════════
function isProMode(){ return localStorage.getItem('ww_pro') === '1'; }
function applyProMode(){
  const on = isProMode();
  document.body.classList.toggle('is-pro', on);
  const btn = document.getElementById('pro-toggle');
  if(btn){
    btn.setAttribute('aria-pressed', String(on));
    btn.title = on ? 'Désactiver le mode Pro' : 'Activer le mode Pro';
    const lbl = btn.querySelector('.pro-toggle-label');
    if(lbl) lbl.textContent = on ? 'PRO' : 'FREE';
  }
}
function toggleProMode(){
  const next = !isProMode();
  localStorage.setItem('ww_pro', next ? '1' : '0');
  applyProMode();
  if(typeof allWallets !== 'undefined' && allWallets.length){
    renderSmartLeaderboard(allWallets);
    applyFilters();
  }
  if(typeof showToast === 'function'){
    showToast(next ? '⚡ Mode Pro activé — Smart Money débloqué' : 'Mode Pro désactivé', next ? 'success' : '', 2200);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Smart Money score
// Préfère le back-end (w.smart_score depuis results.json) sinon fallback JS
// avec mêmes tiers que smart_score.py côté Python.
// ═════════════════════════════════════════════════════════════════════════════
function smartScoreLocal(w){
  const vol   = w.total_volume_usd || 0;
  const nb    = w.dune_nb_trades   || 0;
  const avg   = nb > 0 ? vol / nb : 0;
  const cat   = w.category || 'Unknown';
  let volS = 0;
  if(vol >= 1e9)      volS = 40;
  else if(vol >= 1e8) volS = 32;
  else if(vol >= 1e7) volS = 22;
  else if(vol >= 1e6) volS = 12;
  else if(vol >= 1e5) volS = 5;
  let avgS = 0;
  if(avg >= 5_000_000)     avgS = 10;
  else if(avg >= 1_000_000)avgS = 16;
  else if(avg >= 200_000)  avgS = 22;
  else if(avg >= 50_000)   avgS = 18;
  else if(avg >= 10_000)   avgS = 10;
  else if(avg >= 1_000)    avgS = 4;
  const mevP  = cat === 'MEV Bot' ? -45 : 0;
  const eoaB  = w.is_contract === false ? 6 : 0;
  const spamP = nb > 5000 ? -18 : (nb > 1500 ? -7 : 0);
  const divB  = Math.min(5, (w.unique_tokens_traded || 0) * 0.3);
  const raw = 8 + volS + avgS + mevP + eoaB + spamP + divB;
  return Math.max(0, Math.min(100, Math.round(raw)));
}
function smartScore(w){ return (w && w.smart_score != null) ? w.smart_score : smartScoreLocal(w); }
function smartTier(s){
  if(s >= 75) return { cls: 'smart-score--hi',  label: 'Alpha' };
  if(s >= 55) return { cls: 'smart-score--mid', label: 'Solid' };
  return            { cls: 'smart-score--lo',  label: 'Low'   };
}
// P1.1 backend (cluster detection) — quand plusieurs wallets partagent un
// deployer, ils appartiennent probablement à une seule entité (firme prop,
// bot fleet…). Le leaderboard les dédupe pour éviter qu'un cluster prenne
// 5 places sur 8 et donne l'illusion de 5 alpha indépendants.
function _dedupeClusters(wallets){
  // Garde le wallet au meilleur score par cluster_id ; les autres deviennent
  // invisibles dans le leaderboard mais restent visibles dans la table.
  const bestByCluster = new Map();
  const result = [];
  for(const w of wallets){
    const cid = w.cluster_id;
    if(!cid){ result.push(w); continue; }
    const prev = bestByCluster.get(cid);
    if(!prev || (w._s || 0) > (prev._s || 0)){
      if(prev){
        // Replace prev par w : retire prev de result, ajoute w
        const idx = result.indexOf(prev);
        if(idx >= 0) result.splice(idx, 1);
      }
      bestByCluster.set(cid, w);
      result.push(w);
    }
  }
  return result;
}

function renderSmartLeaderboard(wallets){
  const host = document.getElementById('smart-leaderboard');
  if(!host) return;
  // P1.1 — exclut TOUTE l'infrastructure (CEX, bridges, routers, MM, MEV)
  // pas seulement MEV Bot. Le « Smart Money Leaderboard » ne doit montrer
  // que des wallets dont l'activité ressemble à de l'alpha discrétionnaire.
  let ranked = wallets
    .map(w => ({ ...w, _s: smartScore(w), _type: classifyWalletType(w) }))
    .filter(w => !w._type.infra && w._s >= 40)
    .sort((a,b) => b._s - a._s);
  // Cluster dedup avant le slice → un cluster = une entrée dans le leaderboard
  ranked = _dedupeClusters(ranked).slice(0, 8);
  if(!ranked.length){
    host.innerHTML = '<p class="text-muted fs-sm" style="padding:var(--s-4) 0;text-align:center">Pas encore de wallets passant les filtres infra — lance un Sonar.</p>';
    return;
  }
  host.innerHTML = ranked.map((w, i) => {
    const tier = smartTier(w._s);
    const clusterBadge = (w.cluster_size && w.cluster_size > 1)
      ? `<span class="tag" style="background:rgba(196,125,255,.10);color:#dab5ff;border-color:rgba(196,125,255,.30);font-size:10px" title="Cluster ${w.cluster_id} : ${w.cluster_size} wallets partagent ce deployer">⛓ +${w.cluster_size - 1}</span>`
      : '';
    return `<div class="smart-row" onclick="openModal('${w.address}')" style="cursor:pointer">
      <span class="rank-pill">${i+1}</span>
      <span class="addr">${truncAddr(w.address)}</span>
      <span class="vol">${formatUSDShort(w.total_volume_usd||0)}</span>
      <span class="smart-score ${tier.cls}">${w._s}</span>
      <span class="${w._type.cls}">${w._type.label}</span>
      ${clusterBadge}
    </div>`;
  }).join('');
}

// ═════════════════════════════════════════════════════════════════════════════
// Whale tier — emojis restaurés (brand identity)
// ═════════════════════════════════════════════════════════════════════════════
function whaleTier(vol){
  if(vol>=500e6) return {emoji:'🐳', label:'Mega Whale', cls:'tier--mega',    color:'#dab5ff'};
  if(vol>=100e6) return {emoji:'🐋', label:'Whale',      cls:'tier--whale',   color:'#80edff'};
  if(vol>=10e6)  return {emoji:'🐬', label:'Dolphin',    cls:'tier--dolphin', color:'#9affc1'};
  if(vol>=1e6)   return {emoji:'🐠', label:'Fish',       cls:'tier--fish',    color:'#ffe39a'};
  return               {emoji:'🦐', label:'Shrimp',     cls:'tier--shrimp',  color:'#b8d3e4'};
}

// ═════════════════════════════════════════════════════════════════════════════
// Chart colours
// ═════════════════════════════════════════════════════════════════════════════
const CAT_COLOR = {
  'MEV Bot':       '#ff7a9c',
  'DEX Protocol':  '#7aa9ff',
  'Market Maker':  '#c47dff',
  'Smart Contract':'#ffd970',
  'Other':         '#00e2ff',
  'Unknown':       '#7baacf',
};
function catColor(cat){ return CAT_COLOR[cat]||'#5a8197'; }
function catClass(cat){
  if(cat==='MEV Bot')       return 'tag tag--mev';
  if(cat==='DEX Protocol')  return 'tag tag--dex';
  if(cat==='Market Maker')  return 'tag tag--mm';
  if(cat==='Smart Contract')return 'tag tag--sc';
  if(cat==='Other')         return 'tag tag--other';
  return 'tag tag--unk';
}

// ═════════════════════════════════════════════════════════════════════════════
// P1.1 — Classification granulaire wallet_type (frontend)
// Distingue l'« alpha » (EOA + smart contracts opaques) de l'« infrastructure »
// (CEX, bridges, routers DEX, market makers, MEV bots) pour la fiabilité du
// leaderboard. Détection par regex sur le label + category Dune.
// ═════════════════════════════════════════════════════════════════════════════
const _RX_ROUTER = /(router|aggregator|1inch|paraswap|augustus|0x protocol|cowswap|kyber|metamask swap|universal router|swaprouter|odos|matcha|swap router)/i;
const _RX_BRIDGE = /(bridge|stargate|across|hop|wormhole|synapse|debridge|orbiter|li[\s.-]?fi|connext|settler|mainnet settler|arbitrum settler|optimism settler|base settler)/i;
const _RX_CEX    = /(binance|coinbase|kraken|bybit|okx|kucoin|huobi|bitfinex|gate\.io|crypto\.com|gemini|bitstamp|cex|exchange[: ])/i;
const _RX_MM     = /(market maker|wintermute|jump trading|jane street|amber|gsr|cumberland|flow traders|alameda)/i;
const _RX_MEV    = /(mev|sandwich|jaredfromsubway|frontrunner|backrun|atomic arb)/i;

function classifyWalletType(w){
  const cat = w.category || 'Unknown';
  const lbl = (w.label || '') + ' ' + (w.contract_name || '');
  if(cat === 'MEV Bot' || _RX_MEV.test(lbl))    return { key:'mev',     label:'MEV',     cls:'tag tag--mev',    infra:true };
  if(cat === 'Market Maker' || _RX_MM.test(lbl))return { key:'mm',      label:'MM',      cls:'tag tag--mm',     infra:true };
  if(_RX_CEX.test(lbl))                         return { key:'cex',     label:'CEX',     cls:'tag tag--cex',    infra:true };
  if(_RX_BRIDGE.test(lbl))                      return { key:'bridge',  label:'Bridge',  cls:'tag tag--bridge', infra:true };
  if(cat === 'DEX Protocol' || _RX_ROUTER.test(lbl)) return { key:'router',  label:'Router',  cls:'tag tag--router', infra:true };
  if(cat === 'Smart Contract' || w.is_contract === true) return { key:'contract', label:'Contract', cls:'tag tag--sc',  infra:false };
  return { key:'eoa', label:'EOA', cls:'tag tag--eoa', infra:false };
}
function isInfra(w){ return classifyWalletType(w).infra; }

// ═════════════════════════════════════════════════════════════════════════════
// Charts
// ═════════════════════════════════════════════════════════════════════════════
function initCharts(){
  if (typeof Chart === 'undefined') return; // Lazy-load : sera rappelé via _loadChartJsLazy
  chartBar = new Chart(document.getElementById('chartBar').getContext('2d'),{
    type:'bar',
    data:{labels:[],datasets:[{data:[],backgroundColor:[],borderRadius:4,borderSkipped:false,maxBarThickness:20}]},
    options:{
      indexAxis:'y', responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{
        backgroundColor:'#01060d',borderColor:'#1a558a',borderWidth:1,padding:10,
        titleColor:'#00e2ff',bodyColor:'#dff1ff',
        callbacks:{label:c=>'  ' + formatUSD(c.raw)}
      }},
      scales:{
        x:{grid:{color:'rgba(0,226,255,.05)'},border:{display:false},ticks:{color:'#7baacf',callback:v=>formatUSDShort(v),font:{family:'JetBrains Mono',size:10}}},
        y:{grid:{display:false},border:{display:false},ticks:{color:'#a8cce4',font:{family:'Sora',size:11}}}
      }
    }
  });
  chartPie = new Chart(document.getElementById('chartPie').getContext('2d'),{
    type:'doughnut',
    data:{labels:[],datasets:[{data:[],backgroundColor:[],borderWidth:2,borderColor:'#082a48',hoverOffset:6}]},
    options:{
      responsive:true, maintainAspectRatio:false, cutout:'72%',
      plugins:{
        legend:{display:false},
        tooltip:{backgroundColor:'#01060d',borderColor:'#1a558a',borderWidth:1,padding:10,
          titleColor:'#00e2ff',bodyColor:'#dff1ff',
          callbacks:{label:c=>` ${c.label}: ${c.parsed}`}}
      }
    }
  });
}

const _CATSHORT={'MEV Bot':'MEV','DEX Protocol':'DEX','Market Maker':'MM','Smart Contract':'SC','Unknown':'Unk'};
function _barLabel(w){
  if(w.label&&w.label!=='Unknown') return trunc(w.label,18);
  const cat=_CATSHORT[w.category]||w.category?.slice(0,4)||'?';
  return cat+' '+w.address.slice(0,6)+'…'+w.address.slice(-3);
}
function updateCharts(wallets){
  // No-op si Chart.js n'est pas encore chargé (lazy-load) : sera rappelé
  // par _loadChartJsLazy().onload une fois Chart.js prêt.
  if (typeof Chart === 'undefined' || !chartBar || !chartPie) {
    // Continue quand même pour rendre le legend qui ne dépend pas de Chart.js
    const cats={};
    wallets.forEach(w=>{const c=w.category||'Unknown'; cats[c]=(cats[c]||0)+1;});
    const _sortedFallback = Object.entries(cats).sort((a,b)=>b[1]-a[1]);
    const _catTotal = _sortedFallback.reduce((s,[,n])=>s+n,0);
    const _leg = document.getElementById('legend');
    if (_leg) _leg.innerHTML = _sortedFallback.map(([cat,n])=>{
      const pct=_catTotal>0?Math.round(n/_catTotal*100):0;
      const col=catColor(cat);
      return `<div class="flex items-center gap-2"><span style="width:10px;height:10px;border-radius:2px;background:${col};display:inline-block;flex-shrink:0"></span><span class="fs-xs text-muted">${cat} · ${n} (${pct}%)</span></div>`;
    }).join('');
    return;
  }
  const top10=[...wallets].sort((a,b)=>(b.total_volume_usd||0)-(a.total_volume_usd||0)).slice(0,10);
  chartBar.data.labels = top10.map(w=>_barLabel(w));
  chartBar.data.datasets[0].data = top10.map(w=>w.total_volume_usd||0);
  chartBar.data.datasets[0].backgroundColor = top10.map(w=>catColor(w.category));
  chartBar.update('none');

  const cats={};
  wallets.forEach(w=>{const c=w.category||'Unknown'; cats[c]=(cats[c]||0)+1;});
  const sorted=Object.entries(cats).sort((a,b)=>b[1]-a[1]);
  chartPie.data.labels = sorted.map(e=>e[0]);
  chartPie.data.datasets[0].data = sorted.map(e=>e[1]);
  chartPie.data.datasets[0].backgroundColor = sorted.map(e=>catColor(e[0]));
  chartPie.update('none');

  const catTotal = sorted.reduce((s,[,n])=>s+n,0);
  const leg=document.getElementById('legend');
  leg.innerHTML=sorted.map(([cat,n])=>{
    const pct=catTotal>0?Math.round(n/catTotal*100):0;
    const col=catColor(cat);
    return `<div class="flex items-center justify-between fs-xs">
      <div class="flex items-center gap-2">
        <span style="width:10px;height:10px;border-radius:2px;background:${col}"></span>
        <span style="color:var(--text-soft)">${cat}</span>
      </div>
      <div class="flex items-center gap-2">
        <span class="tabular-nums font-semibold" style="color:${col};min-width:28px;text-align:right">${pct}%</span>
        <div style="width:48px;height:4px;border-radius:2px;background:var(--border)">
          <div style="height:4px;border-radius:2px;width:${pct}%;background:${col};opacity:.85"></div>
        </div>
        <span class="tabular-nums" style="width:28px;text-align:right;color:var(--muted)">${n}</span>
      </div>
    </div>`;
  }).join('');
}

// ═════════════════════════════════════════════════════════════════════════════
// Table
// ═════════════════════════════════════════════════════════════════════════════
function renderTable(wallets){
  document.getElementById('row-count').textContent=`${wallets.length} wallet${wallets.length!==1?'s':''}`;
  const tbody=document.getElementById('tbody');

  if(!wallets.length){
    const q=search?`"${search}"`:catFilter!=='all'?`"${catFilter}"`:'';
    tbody.innerHTML=`<tr><td colspan="8" class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <p style="font-weight:600;color:var(--text)">Aucun résultat${q?' pour '+q:''}</p>
      <p class="fs-xs mt-3">Essaie un autre filtre ou efface la recherche</p>
    </td></tr>`;
    document.getElementById('page-info').textContent='';
    document.getElementById('btn-prev').disabled=true;
    document.getElementById('btn-next').disabled=true;
    return;
  }

  const pages=Math.ceil(wallets.length/PER_PAGE);
  page=Math.min(page,pages);
  const start=(page-1)*PER_PAGE;
  const slice=wallets.slice(start,start+PER_PAGE);

  document.getElementById('page-info').textContent=`Page ${page} / ${pages}`;
  document.getElementById('btn-prev').disabled=page<=1;
  document.getElementById('btn-next').disabled=page>=pages;

  tbody.innerHTML=slice.map(w=>{
    const pct=maxVol>0?Math.round((w.total_volume_usd||0)/maxVol*100):0;
    const tier=whaleTier(w.total_volume_usd||0);
    const rankNum=w.rank<=3
      ? `<span class="font-mono font-bold tabular-nums" style="color:${tier.color};font-size:13px">#${w.rank}</span>`
      : `<span class="font-mono tabular-nums" style="color:var(--muted)">${w.rank}</span>`;
    const tierBadge=`<span class="tier ${tier.cls}" title="${tier.label}"><span class="emoji">${tier.emoji}</span>${tier.label}</span>`;
    // P1.1 — badge type granulaire (EOA/Contract/Router/Bridge/CEX/MM/MEV)
    const wt = classifyWalletType(w);
    const typeBadge = `<span class="${wt.cls} ${wt.infra ? 'tag--infra' : ''}" title="${wt.infra ? 'Infrastructure — pas un signal d’alpha' : 'Wallet potentiellement actif'}">${wt.label}</span>`;
    const label=w.label&&w.label!=='Unknown'?w.label:'Unknown';
    const catBadge='';
    const avgTrade=(w.dune_nb_trades&&w.dune_nb_trades>0)
      ? formatUSD((w.dune_volume_usd||0)/w.dune_nb_trades)
      : null;
    const avgTradeCell=avgTrade
      ? `<span class="font-mono tabular-nums" style="color:var(--cyan)">${avgTrade}</span>`
      : `<span style="color:var(--dim)">—</span>`;
    const trades=w.dune_nb_trades
      ? `<span class="tabular-nums" style="color:var(--text-soft)">${fmtInt(w.dune_nb_trades)}</span>`
      : `<span style="color:var(--dim)">—</span>`;
    const ss = w.smart_score ?? smartScore(w);
    const sTier = smartTier(ss);
    const smartCell = `<td class="smart-col text-right">
      <div class="flex items-center justify-between gap-2" style="justify-content:flex-end">
        <span class="smart-bar"><span class="smart-bar-fill" style="width:${ss}%"></span></span>
        <span class="smart-score ${sTier.cls}">${ss}</span>
      </div>
    </td>`;

    return `<tr class="tbl-row" onclick="openModal('${w.address}')">
      <td>
        <div class="flex items-center gap-2">
          <span style="min-width:24px;text-align:right">${rankNum}</span>
          ${tierBadge}
        </div>
      </td>
      <td>
        <div class="flex items-center gap-2">
          <span class="mono" style="color:var(--blue)">${truncAddr(w.address)}</span>
          <button class="icon-btn" onclick="copyAddr(event,'${w.address}')" title="Copier">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
          ${w.cluster_size && w.cluster_size > 1 ? `<span class="tag" style="background:rgba(196,125,255,.10);color:#dab5ff;border-color:rgba(196,125,255,.30);font-size:10px;padding:1px 6px" title="Cluster ${w.cluster_id} — ${w.cluster_size} wallets partagent ce deployer (probable même entité)">⛓ ${w.cluster_id}</span>` : ''}
        </div>
      </td>
      <td>
        <div class="flex items-center gap-2 flex-wrap">
          ${typeBadge}
          <span style="color:var(--text-soft);font-weight:500">${trunc(label,22)}</span>
          ${catBadge}
        </div>
      </td>
      <td class="text-right">
        <div class="flex items-center justify-between gap-2" style="justify-content:flex-end">
          <span class="font-bold tabular-nums" style="color:${tier.color}">${formatUSD(w.total_volume_usd)}</span>
          <span class="volbar"><span class="volbar-fill" style="width:${pct}%"></span></span>
        </div>
      </td>
      ${smartCell}
      <td class="text-right">${trades}</td>
      <td class="text-right">${avgTradeCell}</td>
      <td class="text-right">
        <div class="flex items-center justify-between gap-1" style="justify-content:flex-end">
          <a href="${currentExplorer()}/address/${w.address}" target="_blank" onclick="event.stopPropagation()" class="icon-btn" title="Etherscan">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
          <a href="https://debank.com/profile/${w.address}" target="_blank" onclick="event.stopPropagation()" class="icon-btn" title="DeBank">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8" stroke-linecap="round"/></svg>
          </a>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ═════════════════════════════════════════════════════════════════════════════
// Count-up animation
// ═════════════════════════════════════════════════════════════════════════════
function countUp(el, target, formatter, duration=900){
  if(!el) return;
  // Fallback : si rAF est gelé (tab en background, headless),
  // garantit que la valeur finale s'affiche quoi qu'il arrive.
  const finalize = () => { el.textContent = formatter(target); };
  setTimeout(finalize, duration + 80);
  const start=performance.now();
  function step(now){
    const t=Math.min((now-start)/duration,1);
    const ease=1-Math.pow(1-t,3);
    el.textContent=formatter(target*ease);
    if(t<1) requestAnimationFrame(step);
    else finalize();
  }
  requestAnimationFrame(step);
}

// ═════════════════════════════════════════════════════════════════════════════
// Sparklines
// ═════════════════════════════════════════════════════════════════════════════
function drawSparkline(id, value, color){
  const lineEl = document.getElementById(id+'-line');
  const fillEl = document.getElementById(id+'-fill');
  if(!lineEl||!fillEl||!value) return;
  const pts=[];
  let v = value*(0.55+Math.random()*0.3);
  for(let i=0;i<13;i++){
    v = Math.max(0, v*(0.88+Math.random()*0.24));
    pts.push(v);
  }
  pts.push(value);
  const min=Math.min(...pts)*0.9, max=Math.max(...pts)*1.02;
  const rng=max-min||1;
  const coords = pts.map((p,i)=>{
    const x=(i/(pts.length-1)*80).toFixed(1);
    const y=(28-(p-min)/rng*26).toFixed(1);
    return `${x},${y}`;
  }).join(' ');
  lineEl.setAttribute('points', coords);
  fillEl.setAttribute('points', coords+` 80,30 0,30`);
}

// ═════════════════════════════════════════════════════════════════════════════
// Stats
// ═════════════════════════════════════════════════════════════════════════════
function updateStats(data){
  const ws=data.wallets||[];
  const totVol = data.total_volume_usd||0;
  countUp(document.getElementById('stat-volume'), totVol, formatUSDShort);
  countUp(document.getElementById('stat-total'),  ws.length, v=>Math.round(v));
  const mevs=ws.filter(w=>w.category==='MEV Bot');
  countUp(document.getElementById('stat-mev'), mevs.length, v=>Math.round(v));
  document.getElementById('stat-mev-vol').textContent=formatUSDShort(mevs.reduce((s,w)=>s+(w.total_volume_usd||0),0))+' vol cumulé';
  const contracts=ws.filter(w=>w.is_contract).length;
  document.getElementById('stat-contracts-sub').textContent=`${contracts} smart contracts`;
  const ep=data.eth_price;
  if(ep) _analysisEthPrice = ep;
  document.getElementById('stat-eth').textContent=fmtPrice(ep);
  document.getElementById('eth-price').textContent=fmtPrice(ep);
  // Hero strip live stats — retire la classe skel pour révéler le texte
  ['hero-wallets','hero-volume','hero-mev'].forEach(id => {
    const el = document.getElementById(id);
    if(el) el.classList.remove('skel','skel--text','skel--num-l');
  });
  document.getElementById('hero-wallets').textContent = ws.length;
  document.getElementById('hero-volume').textContent  = formatUSDShort(totVol);
  document.getElementById('hero-mev').textContent     = mevs.length;
  drawSparkline('spark-volume', totVol, '#5eff9e');
  drawSparkline('spark-total',  ws.length, '#00e2ff');
  drawSparkline('spark-mev',    mevs.length, '#ff7a9c');
  drawSparkline('spark-eth',    ep||0, '#ffd970');
  fetchLivePrices();
  if(data.last_updated){
    _lastUpdatedAt = new Date(data.last_updated).getTime();
    document.getElementById('live-dot')?.classList.remove('hidden');
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Icon helpers (inline SVG strings — used in JS-emitted HTML)
// ═════════════════════════════════════════════════════════════════════════════
function _svg(inner, size=16){
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;flex-shrink:0">${inner}</svg>`;
}
const ICON = {
  pair:    _svg('<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>'),
  dex:     _svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'),
  time:    _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
  size:    _svg('<path d="M3 3v18h18"/><path d="M7 17l4-4 4 2 5-7"/>'),
  special: _svg('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>'),
  whale:   _svg('<path d="M3 12c0-3 2-5 5-5s5 2 5 5"/><path d="M13 12c0-3 2-5 5-5s5 2 5 5-2 5-5 5-5-2-5-5"/>'),
  bolt:    _svg('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>', 14),
  hold:    _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 9 15"/>'),
  hourglass: _svg('<path d="M6 2h12M6 22h12M6 2v4a6 6 0 0 0 12 0V2M6 22v-4a6 6 0 0 1 12 0v4"/>', 14),
};

// ═════════════════════════════════════════════════════════════════════════════
// Patterns
// ═════════════════════════════════════════════════════════════════════════════
// loadPatterns = fast-path patterns-only (n configurable depuis le dropdown).
// Sonar (triggerRefresh) lance le pipeline complet (wallets + patterns).
// Coexistence intentionnelle : permettre de re-calculer juste les patterns avec
// un n différent (100/300/500) sans relancer toute l'analyse Etherscan.
// ═════════════════════════════════════════════════════════════════════════════
async function loadPatterns(forceRefresh=false){
  const btn  = document.getElementById('btn-patterns');
  const body = document.getElementById('patterns-body');
  const n    = parseInt(document.getElementById('patterns-n')?.value || '100');
  btn.disabled = true;
  btn.innerHTML = `<svg class="spin" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10" stroke-dasharray="30 10" opacity=".3"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg> Analyse…`;
  body.innerHTML = `<div class="flex items-center justify-between gap-3 text-muted" style="justify-content:center;padding:var(--s-7) 0">
    <svg class="spin" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10" stroke-dasharray="30 10" opacity=".3"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg>
    <span class="fs-sm">Interrogation Dune — top <strong style="color:var(--text)">${n}</strong> wallets · ~${n<=100?'60':'120'}s…</span>
  </div>`;

  try {
    // Tente cache d'abord (sauf si forceRefresh demandé)
    if (!forceRefresh) {
      const cached = await fetch(`/api/patterns?chain=${encodeURIComponent(_currentChain)}`).then(r => r.ok ? r.json() : null).catch(() => null);
      if (cached && cached.n_wallets === n) {
        renderPatterns(cached); _setPatternsMeta(cached); _resetPatternsBtn();
        return;
      }
    }
    // Sinon déclenche le calcul puis poll
    await fetch(`/api/patterns/refresh?n=${n}`, { method: 'POST' });
    const maxTries = n <= 100 ? 40 : 70;
    let tries = 0, ready = false;
    while (tries++ < maxTries) {
      await new Promise(res => setTimeout(res, 3000));
      const d = await fetch(`/api/patterns?chain=${encodeURIComponent(_currentChain)}`).then(r => r.ok ? r.json() : null).catch(() => null);
      if (d && d.n_wallets === n) {
        renderPatterns(d); _setPatternsMeta(d); ready = true; break;
      }
    }
    if (!ready) throw new Error('Timeout — réessayez');
    _resetPatternsBtn();
  } catch(e) {
    body.innerHTML = `<p class="fs-sm text-center" style="color:var(--c-mev);padding:var(--s-5) 0">${e.message}</p>`;
    btn.innerHTML = 'Réessayer'; btn.disabled = false;
  }
}
function _resetPatternsBtn(){
  const btn = document.getElementById('btn-patterns');
  if(!btn) return;
  btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><polyline points="21 3 21 8 16 8"/></svg> Réanalyser`;
  btn.disabled = false;
}
function _setPatternsMeta(d){
  const meta = document.getElementById('patterns-meta');
  if(meta) meta.textContent = `· ${d.n_wallets} wallets · ${d.days}j · ${(d.generated_at||'').slice(0,16).replace('T',' ')} UTC`;
}

// ─── Patterns: constantes & helpers de rendu ─────────────────────────────────
const INSIGHT_META = {
  pair:           { cls:'insight-row--blue',   label:'Paire',     icon: 'pair' },
  dex:            { cls:'insight-row--purple', label:'DEX',       icon: 'dex' },
  time:           { cls:'insight-row--green',  label:'Timing',    icon: 'time' },
  size:           { cls:'insight-row--yellow', label:'Taille',    icon: 'size' },
  specialization: { cls:'insight-row--red',    label:'Spécia.',   icon: 'special' },
  whale:          { cls:'insight-row--cyan',   label:'Baleine',   icon: 'whale' },
  mev_price:      { cls:'insight-row--yellow', label:'MEV Prix',  icon: 'bolt' },
  hold_time:      { cls:'insight-row--purple', label:'Détention', icon: 'hold' },
};
const DEX_COLORS = {
  uniswap:'#ff007a', aerodrome:'#2563eb', fluid:'#7c3aed', curve:'#f59e0b',
  pancakeswap:'#f97316', prjx:'#06b6d4', velodrome:'#6366f1', sushiswap:'#e11d48',
};
const dexColor = p => DEX_COLORS[p?.toLowerCase()] || '#5a8197';

// Recalcule l'insight "size" depuis size_distribution (corrige cache stale)
function _sizeInsightOverride(d){
  const buckets = d.size_distribution || [];
  if (!buckets.length) return null;
  const totalT = buckets.reduce((s,b)=>s+(b.trade_count||0), 0);
  const totalV = buckets.reduce((s,b)=>s+(b.volume_usd||0),  0);
  const topC   = buckets.reduce((a,b)=>b.trade_count>a.trade_count?b:a);
  const topV   = buckets.reduce((a,b)=>b.volume_usd  >a.volume_usd  ?b:a);
  return {
    title:  `Taille majoritaire : ${topC.bucket || ''}`,
    detail: `${totalT?Math.round(topC.trade_count/totalT*100):0}% des trades par nombre · `
          + `volume dominant : ${topV.bucket} (${totalV?Math.round(topV.volume_usd/totalV*100):0}% du vol)`,
  };
}

function _renderInsights(d) {
  const override = _sizeInsightOverride(d);
  return (d.insights||[]).map(ins => {
    const m = INSIGHT_META[ins.type] || { cls:'insight-row--cyan', label:'', icon:'special' };
    if (ins.type === 'size' && override) ins = { ...ins, ...override };
    let titleHtml = ins.title, liveBadge = '';
    if (ins.type === 'mev_price' && d.mev_price_levels?.buy) {
      const { buy, sell } = d.mev_price_levels;
      titleHtml = `Prix MEV WETH — buy <span id="mev-insight-buy" class="font-mono">${fmtP(buy.median)}</span> `
                + `/ sell <span id="mev-insight-sell" class="font-mono">${fmtP(sell.median)}</span>`;
      liveBadge = ' <span class="live-dot" title="Mise à jour temps réel"></span>';
    }
    return `<div class="insight-row ${m.cls}">
      <div class="insight-icon">${ICON[m.icon]}</div>
      <div style="flex:1;min-width:0">
        <div class="flex items-center gap-2">
          <span class="insight-label">${m.label}</span>${liveBadge}
        </div>
        <p class="insight-title">${titleHtml}</p>
        <p class="insight-detail">${ins.detail}</p>
      </div>
    </div>`;
  }).join('');
}

function _renderHourlyHeatmap(d) {
  const data = d.hourly_activity || [];
  // max=1 et min=0 : guards pour data vide (évite -Infinity/+Infinity dans la normalisation)
  const maxT = Math.max(...data.map(h=>h.trades), 1);
  const minT = Math.min(...data.map(h=>h.trades), 0);
  const peak = data.reduce((a,b)=>b.trades>a.trades?b:a, {trades:0,hour:0});
  const html = Array.from({length:24}, (_,h) => {
    const row  = data.find(r=>r.hour===h) || {trades:0};
    const norm = maxT > minT ? (row.trades - minT) / (maxT - minT) : 0;
    const isPeak = h === peak.hour;
    const barH = Math.round(8 + norm * 44);
    const color = isPeak ? 'var(--green)' : `rgba(75,212,255,${(0.15 + norm*0.55).toFixed(2)})`;
    return `<div class="hour-bar" title="${h}h UTC — ${fmtNum(row.trades)} trades${isPeak?' · PIC':''}">
      <div class="hour-bar-fill" style="height:${barH}px;background:${color}"></div>
      <span class="hour-bar-label" style="${isPeak?'color:var(--green);font-weight:700':''}">${h%4===0?h+'h':isPeak?'▲':''}</span>
    </div>`;
  }).join('');
  return { html, peak };
}

function _renderSizeBuckets(d) {
  const buckets = d.size_distribution || [];
  const maxT  = Math.max(...buckets.map(b=>b.trade_count), 1);
  const maxV  = Math.max(...buckets.map(b=>b.volume_usd),  1);
  const totalV = buckets.reduce((s,b)=>s+(b.volume_usd||0), 0);
  return buckets.map(b => {
    const pctT = Math.round(b.trade_count / maxT * 100);
    const pctV = Math.round(b.volume_usd  / maxV * 100);
    const share= Math.round(b.volume_usd  / totalV * 100);
    return `<div class="dist-row">
      <div class="flex items-center justify-between mb-3">
        <span class="text-soft font-semibold" style="width:90px">${b.bucket}</span>
        <div class="flex items-center gap-3 fs-xs">
          <span class="tabular-nums text-muted">${fmtNum(b.trade_count)} trades</span>
          <span class="font-semibold tabular-nums text-green" style="width:60px;text-align:right">${formatUSD(b.volume_usd)}</span>
          <span class="tabular-nums text-muted" style="width:32px;text-align:right">${share}%</span>
        </div>
      </div>
      <div class="flex flex-col gap-1">
        <div class="dist-bar"><div class="dist-bar-fill" style="width:${pctT}%;background:var(--c-sc)"></div></div>
        <div class="dist-bar"><div class="dist-bar-fill" style="width:${pctV}%;background:var(--cyan)"></div></div>
      </div>
    </div>`;
  }).join('');
}

function _renderTopPairs(d) {
  const maxV = d.top_pairs?.[0]?.volume_usd || 1;
  return (d.top_pairs||[]).slice(0,8).map((p,i) => {
    const pct = Math.round(p.volume_usd / maxV * 100);
    const dc  = dexColor(p.project);
    return `<div style="padding:var(--s-3) 0;border-bottom:1px solid var(--border)">
      <div class="flex items-center justify-between mb-3 gap-2">
        <div class="flex items-center gap-2">
          <span class="text-muted fs-xs" style="width:14px">${i+1}</span>
          <span class="font-mono fs-sm font-semibold" style="color:var(--text)">${p.pair}</span>
          <span style="padding:2px 8px;border-radius:5px;font-size:10px;font-weight:600;background:${dc}22;color:${dc};border:1px solid ${dc}44">${p.project}</span>
        </div>
        <span class="font-semibold tabular-nums text-green fs-sm">${formatUSD(p.volume_usd)}</span>
      </div>
      <div class="flex items-center gap-2" style="padding-left:22px">
        <div class="dist-bar" style="flex:1"><div class="dist-bar-fill" style="width:${pct}%;background:${dc}"></div></div>
        <span class="text-muted fs-xs tabular-nums">${fmtNum(p.total_trades)} trades · avg ${formatUSD(p.avg_usd)}</span>
      </div>
    </div>`;
  }).join('');
}

function _renderTopDex(d) {
  const maxV = d.top_dexes?.[0]?.volume_usd || 1;
  return (d.top_dexes||[]).slice(0,6).map(x => {
    const pct = Math.round(x.volume_usd / maxV * 100);
    const dc  = dexColor(x.project);
    return `<div class="flex items-center gap-2 fs-sm" style="padding:6px 0">
      <span style="width:8px;height:8px;border-radius:50%;background:${dc};flex-shrink:0"></span>
      <span class="text-soft font-semibold" style="width:90px">${x.project}</span>
      <div class="dist-bar" style="flex:1;height:6px"><div class="dist-bar-fill" style="width:${pct}%;background:${dc};opacity:.85"></div></div>
      <span class="text-soft tabular-nums" style="width:60px;text-align:right">${formatUSD(x.volume_usd)}</span>
      <span class="text-muted tabular-nums" style="width:28px;text-align:right">${x.wallet_count}w</span>
    </div>`;
  }).join('');
}

// Met à jour les globals live-MEV (consommés par _setPriceEl à chaque tick CoinGecko)
function _updateMevRefs(mev) {
  if (!mev?.buy || !mev?.sell) return;
  _mevBuyMedian  = mev.buy.median;
  _mevSellMedian = mev.sell.median;
  _mevRefEth     = _analysisEthPrice || _prevPrices['eth-price'] || mev.buy.median;
}

function _renderMevPriceBar(mev) {
  if (!mev?.buy || !mev?.sell) return '';
  const b = mev.buy, s = mev.sell;
  const pMin = Math.min(b.p10, s.p10), pMax = Math.max(b.p90, s.p90);
  const rng = pMax - pMin || 1;
  const toX = v => Math.max(0, Math.min(100, ((v - pMin) / rng * 100))).toFixed(1);
  const bW  = v => Math.max(1, ((v.p75 - v.p25) / rng * 100)).toFixed(1);
  const spread = mev.spread_usd;
  const spreadColor = spread >= 0 ? '#2ed4a8' : '#ff6b87';
  const arrow = spread >= 0 ? '↑' : '↓';

  const barRow = (side, data, col) => `
    <div class="flex items-center gap-3 fs-sm">
      <span style="width:36px;text-align:right;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:${col};font-size:11px;flex-shrink:0">${side}</span>
      <div style="flex:1;position:relative;height:24px">
        <div style="position:absolute;left:0;right:0;top:50%;height:1px;background:var(--border-2)"></div>
        <div style="position:absolute;top:50%;transform:translateY(-50%);height:14px;border-radius:3px;left:${toX(data.p10)}%;width:${((data.p90-data.p10)/rng*100).toFixed(1)}%;background:${col}14;border:1px solid ${col}22"></div>
        <div style="position:absolute;top:50%;transform:translateY(-50%);height:14px;border-radius:3px;left:${toX(data.p25)}%;width:${bW(data)}%;background:${col};opacity:.35"></div>
        <div style="position:absolute;top:0;bottom:0;width:2px;border-radius:1px;left:${toX(data.median)}%;transform:translateX(-50%);background:${col}"></div>
      </div>
      <div class="flex items-center gap-2" style="flex-shrink:0;text-align:right">
        <span id="mev-${side.toLowerCase()}-median" class="font-mono font-bold" style="color:${col}">${fmtP(data.median)}</span>
        <span class="text-muted fs-xs">${fmtNum(data.trade_count)} trades</span>
      </div>
    </div>`;

  const ticks = Array.from({length:5}, (_,i) => {
    const v = pMin + (rng * i / 4);
    return `<span style="left:${(i/4*100).toFixed(1)}%;transform:translateX(-50%);position:absolute">${fmtP(v)}</span>`;
  }).join('');

  return `
  <div class="mt-5" style="padding-top:var(--s-5);border-top:1px solid var(--border)">
    <div style="padding:var(--s-4);border-radius:var(--radius-md);background:rgba(255,198,87,.04);border:1px solid rgba(255,198,87,.18)">
      <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p class="fs-xs uppercase tracking-wide font-semibold flex items-center gap-2" style="color:var(--c-sc)">
          ${ICON.bolt} Prix d'entrée MEV
          <span style="padding:2px 7px;border-radius:5px;font-family:'Fira Code',monospace;font-size:10px;font-weight:700;background:rgba(255,198,87,.15);color:var(--c-sc);border:1px solid rgba(255,198,87,.30)">WETH</span>
          <span class="live-dot"></span>
          <span style="text-transform:none;font-weight:400;color:var(--muted);font-size:10px">(wallets ≥200 trades/7j)</span>
        </p>
        <span class="fs-xs font-mono font-semibold" style="padding:2px 8px;border-radius:5px;color:${spreadColor};background:${spreadColor}18;border:1px solid ${spreadColor}33">spread ${arrow}${fmtP2(Math.abs(spread))} (${spread>=0?'+':''}${mev.spread_bps} bps)</span>
      </div>
      <div class="flex flex-col gap-3">
        ${barRow('BUY',  b, '#2ed4a8')}
        ${barRow('SELL', s, '#ff6b87')}
        <div style="position:relative;font-size:9.5px;color:var(--muted-2);font-family:'Fira Code',monospace;height:16px;padding-left:40px;padding-right:90px">${ticks}</div>
      </div>
      <div class="flex items-center justify-between fs-xs mt-3" style="color:var(--muted)">
        <span>Bande = P25–P75 · trait = médiane · fond = P10–P90</span>
        <span>Δ buy→sell : <span class="font-mono" style="color:${spreadColor}">${spread>=0?'+':''}${fmtP2(spread)}</span></span>
      </div>
    </div>
  </div>`;
}

function _renderHoldTime(d) {
  const dist = d.hold_time_distribution || [];
  if (!dist.length) return '';
  const totalFlips = dist.reduce((s,r) => s + r.flip_count, 0);
  const maxFlips   = Math.max(...dist.map(r => r.flip_count), 1);
  const cumFast    = dist.filter(r => r.avg_hold_sec <= 13).reduce((s,r) => s + r.pct, 0);
  const bucketColor = b =>
    b.includes('bloc') ? '#2ed4a8' : b.includes('1min') ? '#4cd4ff' :
    b.includes('5min') ? '#a3e635' : b.includes('1h')   ? '#ffc657' :
    b.includes('24h')  ? '#ff9663' : '#ff6b87';
  const fmtSec = s =>
    s < 60   ? `${Math.round(s)}s` :
    s < 3600 ? `${Math.floor(s/60)}m${Math.round(s%60)}s` :
               `${Math.floor(s/3600)}h${Math.round((s%3600)/60)}m`;
  const bars = dist.map(r => {
    const pct = Math.round(r.flip_count / maxFlips * 100);
    const col = bucketColor(r.bucket);
    const med = r.median_hold_sec > 0 ? `médiane ${fmtSec(r.median_hold_sec)}` : '';
    return `<div class="flex items-center gap-2 fs-sm">
      <span style="width:120px;text-align:right;color:var(--text-soft);font-weight:500;flex-shrink:0">${r.bucket}</span>
      <div style="flex:1;position:relative;height:20px;display:flex;align-items:center">
        <div style="position:absolute;top:0;bottom:0;left:0;border-radius:0 3px 3px 0;width:${pct}%;background:${col};opacity:.8;min-width:2px"></div>
        <span style="position:relative;padding-left:8px;color:var(--text);font-weight:600">${fmtNum(r.flip_count)}</span>
      </div>
      <span class="tabular-nums font-bold" style="width:32px;text-align:right;color:${col}">${r.pct}%</span>
      <span class="text-muted tabular-nums" style="width:80px;text-align:right">${med}</span>
    </div>`;
  }).join('');
  return `
  <div class="mt-4">
    <div style="padding:var(--s-4);border-radius:var(--radius-md);background:rgba(182,117,238,.04);border:1px solid rgba(182,117,238,.18)">
      <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p class="fs-xs uppercase tracking-wide font-semibold flex items-center gap-2" style="color:var(--c-mm)">
          ${ICON.hourglass} Temps de détention MEV
          <span style="padding:2px 7px;border-radius:5px;font-family:'Fira Code',monospace;font-size:10px;font-weight:700;background:rgba(255,198,87,.15);color:var(--c-sc);border:1px solid rgba(255,198,87,.30)">WETH buy→sell</span>
          <span style="text-transform:none;font-weight:400;color:var(--muted);font-size:10px">(même wallet)</span>
        </p>
        <span class="fs-xs tabular-nums font-mono text-muted">${fmtNum(totalFlips)} paires</span>
      </div>
      <div class="flex flex-col gap-3 mb-3">${bars}</div>
      <div class="flex items-center justify-between fs-xs">
        <span class="text-muted"><span class="font-bold text-green">${cumFast.toFixed(0)}%</span> des flips dans le même bloc Ethereum <span style="opacity:.7">(sandwich pur)</span></span>
        <span class="text-muted">1 bloc ≈ 12s</span>
      </div>
    </div>
  </div>`;
}

function renderPatterns(d) {
  // ── Effet de bord explicite : sync les refs live MEV ──
  _updateMevRefs(d.mev_price_levels);

  // ── Rendu pur en helpers ciblés ──
  const insightsHtml = _renderInsights(d);
  const { html: hours, peak: peakHour } = _renderHourlyHeatmap(d);
  const bucketsHtml = _renderSizeBuckets(d);
  const mevHtml     = _renderMevPriceBar(d.mev_price_levels);
  const holdHtml    = _renderHoldTime(d);
  const pairsHtml   = _renderTopPairs(d);
  const dexHtml     = _renderTopDex(d);

  // ── Badge nb signaux ──
  const sigCount = document.getElementById('patterns-signal-count');
  if (sigCount && (d.insights||[]).length) {
    sigCount.textContent = `${d.insights.length} signaux`;
    sigCount.classList.remove('hidden');
  }

  // ── Assemblage ──
  document.getElementById('patterns-body').innerHTML = `
    <div class="patterns-grid">
      <div>
        <p class="fs-xs uppercase tracking-wide font-semibold mb-3 text-muted">SIGNAUX DÉTECTÉS</p>
        ${insightsHtml}
      </div>
      <div class="flex flex-col gap-5">
        <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(59,166,255,.04);border:1px solid rgba(59,166,255,.15)">
          <div class="flex items-center justify-between mb-3">
            <p class="fs-xs uppercase tracking-wide font-semibold text-soft">Activité UTC</p>
            <span class="fs-xs font-semibold text-green">▲ pic ${peakHour.hour}h · ${fmtNum(peakHour.trades)}</span>
          </div>
          <div class="flex items-end gap-1" style="width:100%">${hours}</div>
        </div>
        <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(255,198,87,.04);border:1px solid rgba(255,198,87,.15)">
          <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
            <p class="fs-xs uppercase tracking-wide font-semibold" style="color:var(--c-sc)">Taille des trades</p>
            <div class="flex items-center gap-3 fs-xs">
              <span class="flex items-center gap-2"><span style="width:12px;height:5px;border-radius:2px;background:var(--c-sc)"></span><span class="text-muted">Nombre</span></span>
              <span class="flex items-center gap-2"><span style="width:12px;height:5px;border-radius:2px;background:var(--cyan)"></span><span class="text-muted">Volume</span></span>
            </div>
          </div>
          <div class="flex flex-col gap-4">${bucketsHtml}</div>
        </div>
      </div>
    </div>
    ${mevHtml}
    ${holdHtml}
    <div class="grid-2 mt-5" style="padding-top:var(--s-5);border-top:1px solid var(--border)">
      <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(59,166,255,.04);border:1px solid rgba(59,166,255,.15)">
        <p class="fs-xs uppercase tracking-wide font-semibold mb-3 text-soft">Top Paires <span class="text-muted" style="text-transform:none;font-weight:400;font-size:10px">volume · DEX · wallets</span></p>
        ${pairsHtml}
      </div>
      <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(182,117,238,.04);border:1px solid rgba(182,117,238,.15)">
        <p class="fs-xs uppercase tracking-wide font-semibold mb-3" style="color:var(--c-mm)">Top DEX</p>
        <div class="flex flex-col gap-1">${dexHtml}</div>
      </div>
    </div>`;

  _lastPatterns = d;
  window._lastPatterns = d;   // expose pour app.js (auto-refresh tick)
  renderTradingZones();
}

// ═════════════════════════════════════════════════════════════════════════════
// Trading zones
// ═════════════════════════════════════════════════════════════════════════════
let _lastPatterns = null;
// Alias raccourcis pour la lisibilité (cf. WW_CONFIG)
const FRESHNESS_OK_H   = WW_CONFIG.FRESHNESS_OK_H;
const FRESHNESS_WARN_H = WW_CONFIG.FRESHNESS_WARN_H;

function _ageOfPatterns(d){
  const ts = d?.generated_at;
  if(!ts) return { hours: Infinity, label: 'inconnu', state: 'stale' };
  const ms = Date.now() - new Date(ts).getTime();
  if(!isFinite(ms) || ms < 0) return { hours: Infinity, label: 'inconnu', state: 'stale' };
  const mins = ms / 60000, hours = mins / 60, days = hours / 24;
  const label = mins < 60   ? `il y a ${Math.round(mins)}min`
              : hours < 24  ? `il y a ${Math.round(hours)}h`
              : days  < 30  ? `il y a ${Math.round(days)}j`
              :                'obsolète';
  const state = hours < FRESHNESS_OK_H   ? 'ok'
              : hours < FRESHNESS_WARN_H ? 'warn'
              :                            'stale';
  return { hours, label, state };
}

function renderTradingZones(){
  const card = document.getElementById('zones-card');
  if(!card) return;
  const mev = _lastPatterns?.mev_price_levels;
  if(!mev?.buy || !mev?.sell){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const $ = id => document.getElementById(id);

  // ── Freshness chip ──
  const age = _ageOfPatterns(_lastPatterns);
  const chip = $('zones-freshness');
  if(chip){
    chip.hidden = false;
    chip.textContent = age.label;
    chip.classList.remove('is-warn','is-stale');
    if(age.state === 'warn')  chip.classList.add('is-warn');
    if(age.state === 'stale') chip.classList.add('is-stale');
  }
  const isStale = age.state === 'stale';
  card.classList.toggle('is-stale-data', isStale);

  const live  = _prevPrices['eth-price'] || _analysisEthPrice || mev.buy.median;

  const buy = mev.buy, sell = mev.sell;
  const setSide = (prefix, q) => {
    ['p10','p25','median','p75','p90'].forEach(k => { const el = $(`${prefix}-${k}`); if(el) el.textContent = fmtP(q[k]); });
    $(`${prefix}-trades`).textContent = (q.trade_count||0).toLocaleString('en-US') + ' trades';
  };
  setSide('zone-buy',  buy);
  setSide('zone-sell', sell);

  $('zones-current').textContent     = fmtP(live);
  $('zones-trade-count').textContent = ((buy.trade_count||0) + (sell.trade_count||0)).toLocaleString('en-US');

  const bMed = buy.median,  bP25 = buy.p25,  bP75 = buy.p75;
  const sMed = sell.median, sP25 = sell.p25, sP75 = sell.p75;
  const pctVsBuy  = ((live - bMed) / bMed)  * 100;
  const pctVsSell = ((live - sMed) / sMed) * 100;

  let color, tag, title, detail, alertCls;
  if (isStale) {
    color='#ff6b87'; alertCls='alert--danger'; tag='OBSOLÈTE';
    title='Données whales trop anciennes — signal désactivé';
    detail=`Dernière analyse Dune ${age.label}. Clique « Sonar » pour rafraîchir les niveaux BUY/SELL avant de trader.`;
  }
  else if (live < bP25)   { color='#2ed4a8'; alertCls='alert--success'; tag='STRONG BUY';  title="Sous la zone d'accumulation whale";        detail=`Live ${fmtP(live)} sous P25 BUY ${fmtP(bP25)} — opportunité rare, ${fmtPct(pctVsBuy)} vs médiane whale`; }
  else if (live <= bP75)  { color='#2ed4a8'; alertCls='alert--success'; tag='BUY ZONE';    title="Dans la zone d'accumulation whale";        detail=`Live ${fmtP(live)} entre P25 ${fmtP(bP25)} et P75 ${fmtP(bP75)} — c'est ici que les bots entrent`; }
  else if (live < sP25)   { color='#ffc657'; alertCls='alert--warn';    tag='NEUTRAL';     title="Au-dessus du BUY, sous le SELL — attendre"; detail=`${fmtPct(pctVsBuy)} vs médiane BUY · ${fmtPct(pctVsSell)} vs médiane SELL`; }
  else if (live <= sP75)  { color='#ff6b87'; alertCls='alert--danger';  tag='SELL ZONE';   title='Dans la zone de distribution whale';       detail=`Live ${fmtP(live)} entre P25 ${fmtP(sP25)} et P75 ${fmtP(sP75)} — les bots vendent ici`; }
  else                    { color='#ff6b87'; alertCls='alert--danger';  tag='STRONG SELL'; title='Au-dessus de la zone de distribution';     detail=`Live ${fmtP(live)} au-dessus P75 SELL ${fmtP(sP75)} — risque de correction, ${fmtPct(pctVsSell)} vs médiane`; }

  $('zones-reco-title').textContent = title;
  $('zones-reco-detail').textContent = detail;
  const reco = $('zones-reco');
  reco.className = 'alert ' + alertCls + ' mb-4';
  const t = $('zones-reco-tag');
  t.textContent = tag;
  t.style.background = color + '18';
  t.style.color = color;
  t.style.borderColor = color + '40';

  $('zone-buy-action').innerHTML  = `Live ${fmtP(live)} · <span class="font-mono font-bold" style="color:${pctVsBuy < 0 ? '#2ed4a8' : '#ffc657'}">${fmtPct(pctVsBuy)}</span> vs médiane BUY`;
  $('zone-sell-action').innerHTML = `Live ${fmtP(live)} · <span class="font-mono font-bold" style="color:${pctVsSell > 0 ? '#ff6b87' : '#ffc657'}">${fmtPct(pctVsSell)}</span> vs médiane SELL`;

  const all = [buy.p10, buy.p90, sell.p10, sell.p90, live];
  const lo = Math.min(...all) * 0.998, hi = Math.max(...all) * 1.002, span = hi - lo;
  const pos = v => (((v - lo) / span) * 90 + 5).toFixed(1) + '%';
  const wid = (a,b) => (((b - a) / span) * 90).toFixed(1) + '%';
  const pl = $('price-ladder');
  if(pl) pl.innerHTML = `
    <div class="price-ladder-axis"></div>
    <div class="price-ladder-band price-ladder-buy"  style="left:${pos(buy.p25)};width:${wid(buy.p25, buy.p75)}"  title="BUY P25–P75 : ${fmtP(buy.p25)}–${fmtP(buy.p75)}"></div>
    <div class="price-ladder-band price-ladder-sell" style="left:${pos(sell.p25)};width:${wid(sell.p25, sell.p75)}" title="SELL P25–P75 : ${fmtP(sell.p25)}–${fmtP(sell.p75)}"></div>
    <div class="price-ladder-tick buy"  style="left:${pos(buy.p10)}" title="BUY P10"></div>
    <div class="price-ladder-tick buy"  style="left:${pos(buy.p90)}" title="BUY P90"></div>
    <div class="price-ladder-tick sell" style="left:${pos(sell.p10)}" title="SELL P10"></div>
    <div class="price-ladder-tick sell" style="left:${pos(sell.p90)}" title="SELL P90"></div>
    <span class="price-ladder-mlabel buy"  style="left:${pos(bMed)}">BUY ${fmtP(bMed)}</span>
    <span class="price-ladder-mlabel sell" style="left:${pos(sMed)}">SELL ${fmtP(sMed)}</span>
    <div class="price-ladder-current" data-price="${fmtP(live)}" style="left:${pos(live)}"></div>
  `;
}

// ═════════════════════════════════════════════════════════════════════════════
// Live prices
// ═════════════════════════════════════════════════════════════════════════════
const _prevPrices = {};
function _setPriceEl(id, raw, str){
  const el = document.getElementById(id);
  if(!el) return;
  el.textContent = str;
  _prevPrices[id] = raw;
  if(id === 'eth-price' && _mevRefEth > 0 && _mevBuyMedian > 0){
    const delta = raw - _mevRefEth;
    const setIf = (el, val) => { if(el) el.textContent = fmtP(val); };
    setIf(document.getElementById('mev-buy-median'),  _mevBuyMedian  + delta);
    setIf(document.getElementById('mev-sell-median'), _mevSellMedian + delta);
    setIf(document.getElementById('mev-insight-buy'),  _mevBuyMedian  + delta);
    setIf(document.getElementById('mev-insight-sell'), _mevSellMedian + delta);
    if(_lastPatterns) renderTradingZones();
  }
}
async function fetchLivePrices(){
  try{
    const r=await fetch('https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=usd');
    const d=await r.json();
    if(d?.bitcoin?.usd){
      const raw=d.bitcoin.usd;
      _setPriceEl('btc-price', raw, fmtPrice(raw));
      _setPriceEl('stat-btc',  raw, fmtPrice(raw));
    }
    if(d?.ethereum?.usd){
      const raw=d.ethereum.usd;
      _setPriceEl('eth-price', raw, fmtPrice(raw));
      _setPriceEl('stat-eth',  raw, fmtPrice(raw));
    }
  }catch(e){}
}

// ═════════════════════════════════════════════════════════════════════════════
// Auto refresh
// ═════════════════════════════════════════════════════════════════════════════
let _lastUpdatedAt = null;
// Auto-refresh manuel retiré (Sonars lancés par le cron côté serveur).
// On garde _nextRefreshAt à null pour ne pas casser les helpers qui le lisent.
let _nextRefreshAt = null;

function _humanAge(ms){
  const sec = Math.floor(ms/1000);
  if(sec < 60)        return `il y a ${sec}s`;
  if(sec < 3600)      return `il y a ${Math.floor(sec/60)}m`;
  if(sec < 86400)     return `il y a ${Math.floor(sec/3600)}h`;
  return `il y a ${Math.floor(sec/86400)}j`;
}
function _startLiveCounter(){
  setInterval(()=>{
    const el = document.getElementById('last-updated');
    const cd = document.getElementById('refresh-countdown');
    const chip = document.getElementById('live-chip');
    const now = Date.now();
    if(_lastUpdatedAt){
      const age = _humanAge(now - _lastUpdatedAt);
      if(el) el.textContent = age;
      // Met à jour la live-chip avec freshness data
      if(chip){
        const a = (CHAIN_ACCENT[_currentChain] || CHAIN_ACCENT.ethereum);
        chip.textContent = `LIVE · ${a.label} · ${age}`;
      }
    } else if(chip){
      const a = (CHAIN_ACCENT[_currentChain] || CHAIN_ACCENT.ethereum);
      chip.textContent = `LIVE · 7j · ${a.label}`;
    }
    if(cd && _nextRefreshAt){
      const remain = _nextRefreshAt - now;
      if(remain > 0){
        const m = Math.floor(remain/60000);
        const s = String(Math.floor((remain%60000)/1000)).padStart(2,'0');
        cd.textContent = `· ${m}:${s}`;
      } else { cd.textContent = '· ↻'; }
    }
  }, 1000);
}

function _startPriceRefresh(){ setInterval(fetchLivePrices, WW_CONFIG.PRICE_REFRESH_MS); }

// ═════════════════════════════════════════════════════════════════════════════
// Crypto Ticker — top 10 cryptos par market cap via CoinGecko
// Logo + symbole + prix + variation 24h, scroll infini CSS, refresh 60s
// ═════════════════════════════════════════════════════════════════════════════
const CT_FALLBACK = [
  { symbol: 'btc', name: 'Bitcoin',  price: 63500, change: +1.2,
    image: 'https://assets.coingecko.com/coins/images/1/large/bitcoin.png' },
  { symbol: 'eth', name: 'Ethereum', price: 1770,  change: -0.8,
    image: 'https://assets.coingecko.com/coins/images/279/large/ethereum.png' },
];
async function loadCryptoTicker(){
  const track = document.getElementById('crypto-ticker-track');
  if(!track) return;
  let coins = [];
  try {
    const r = await fetch('https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=10&page=1&sparkline=false&price_change_percentage=24h');
    if(r.ok) coins = await r.json();
  } catch(e) {}
  if(!coins.length) coins = CT_FALLBACK;
  const fmt = v => v >= 1000 ? '$' + Math.round(v).toLocaleString('en-US')
                 : v >= 1     ? '$' + v.toFixed(2)
                 : v >= 0.001 ? '$' + v.toFixed(4)
                 :              '$' + v.toFixed(8);
  const html = coins.map((c, i) => {
    const ch = c.price_change_percentage_24h ?? c.change ?? 0;
    const dir = ch >= 0 ? 'up' : 'down';
    const sign = ch >= 0 ? '+' : '';
    // A11y : arrow glyph en plus de la couleur (✓ daltoniens), aria-label pour SR
    const arrow = ch >= 0 ? '▲' : '▼';
    const sym = (c.symbol || '').toUpperCase();
    const price = fmt(c.current_price || c.price || 0);
    const a11y = `${sym} ${price}, variation 24h ${sign}${ch.toFixed(2)}%`;
    return `<a class="ct-item" href="https://www.coingecko.com/en/coins/${c.id || c.symbol}" target="_blank" rel="noopener" aria-label="${a11y}">
      <span class="ct-rank">${String(i+1).padStart(2,'0')}</span>
      <img class="ct-logo" src="${c.image}" alt="" loading="lazy" width="14" height="14"/>
      <span class="ct-sym">${sym}</span>
      <span class="ct-price">${price}</span>
      <span class="ct-delta ${dir}" aria-hidden="true"><span style="font-size:9px;margin-right:2px">${arrow}</span>${sign}${ch.toFixed(2)}%</span>
    </a>`;
  }).join('');
  // Duplicate pour scroll infini sans jump
  track.innerHTML = html + html;

  // Sync header-prices compat (eth-price / btc-price utilisés par les autres scripts)
  const eth = coins.find(c => (c.symbol || '').toLowerCase() === 'eth');
  const btc = coins.find(c => (c.symbol || '').toLowerCase() === 'btc');
  if(eth) { const el = document.getElementById('eth-price'); if(el) el.textContent = fmt(eth.current_price); _prevPrices['eth-price'] = eth.current_price; }
  if(btc) { const el = document.getElementById('btc-price'); if(el) el.textContent = fmt(btc.current_price); _prevPrices['btc-price'] = btc.current_price; }
}
function _startCryptoTicker(){
  loadCryptoTicker();
  if(window._tickerTimer) clearInterval(window._tickerTimer);
  window._tickerTimer = setInterval(loadCryptoTicker, 60_000);
}

// ═════════════════════════════════════════════════════════════════════════════
// Alertes temps réel (migré depuis pro.html — feed /api/alerts polling 30s)
// ═════════════════════════════════════════════════════════════════════════════
const ALERT_ICONS = {
  whale: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12c0-3 2-5 5-5s5 2 5 5"/><path d="M13 12c0-3 2-5 5-5s5 2 5 5-2 5-5 5-5-2-5-5"/></svg>',
  smart: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  mev:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2L13 10M21 2v6M21 2h-6M3 22l8-8M3 22v-6M3 22h6"/></svg>',
  new:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/></svg>',
};
let _seenAlertKeys = new Set();
function _alertAgo(iso){
  if(!iso) return 'récemment';
  const t = Date.parse(iso);
  if(isNaN(t)) return 'récemment';
  const d = (Date.now() - t) / 1000;
  if(d < 60)    return 'il y a ' + Math.max(1, Math.round(d)) + 's';
  if(d < 3600)  return 'il y a ' + Math.round(d/60) + ' min';
  if(d < 86400) return 'il y a ' + Math.round(d/3600) + ' h';
  return 'il y a ' + Math.round(d/86400) + ' j';
}
function renderAlertFeed(alerts, generatedAt){
  const host = document.getElementById('alert-feed');
  if(!host) return;
  const meta = document.getElementById('alerts-meta');
  if(meta) meta.textContent = generatedAt ? ('· ' + _alertAgo(generatedAt)) : '';
  if(!alerts || !alerts.length){
    host.innerHTML = '<p class="text-muted text-center fs-sm" style="padding:var(--s-5) 0">Aucune alerte récente.</p>';
    return;
  }
  const ranked = [...alerts].sort((a,b)=>{
    const sevRank = s => ({high:0, med:1, low:2}[s] ?? 3);
    const sa = sevRank(a.severity), sb = sevRank(b.severity);
    if(sa !== sb) return sa - sb;
    return (Date.parse(b.ts_iso) || 0) - (Date.parse(a.ts_iso) || 0);
  }).slice(0, 12);
  host.innerHTML = ranked.map(a => {
    const key = (a.type||'') + '|' + (a.address||'') + '|' + (a.title||'');
    const isNew = !_seenAlertKeys.has(key);
    if(isNew) _seenAlertKeys.add(key);
    const sevDot = a.severity === 'high' ? '<span class="alert-sev-dot" title="haute"></span>' : '';
    return `<div class="alert-item alert-item--${a.type||'new'}${isNew ? ' alert-item--new-flash' : ''}"
                 ${a.address ? `onclick="window.open(currentExplorer()+'/address/${a.address}', '_blank')"` : ''}>
      <div class="alert-icon">${ALERT_ICONS[a.type] || ALERT_ICONS.new}</div>
      <div class="alert-body">
        <div class="alert-title">${a.title || 'Alerte'}${sevDot}</div>
        <div class="alert-detail">${a.detail || ''}</div>
      </div>
      <span class="alert-time">${_alertAgo(a.ts_iso)}</span>
    </div>`;
  }).join('');
}
async function loadAlerts(){
  const card = document.getElementById('alerts-card');
  // Les alertes (snapshot diff over time) ne sont calculées que pour Ethereum
  // pour l'instant — sur les autres chains, on cache le panel.
  if(_currentChain !== 'ethereum'){
    if(card) card.style.display = 'none';
    return;
  }
  if(card) card.style.display = '';
  try{
    const r = await fetch('/api/alerts');
    if(!r.ok) return;
    const d = await r.json();
    renderAlertFeed(d.alerts || [], d.generated_at);
  }catch(e){}
}
function _startAlertsPolling(){
  if(window._alertsTimer) clearInterval(window._alertsTimer);
  loadAlerts();
  window._alertsTimer = setInterval(loadAlerts, 30_000);
}

// ═════════════════════════════════════════════════════════════════════════════
// Filters / sort / pagination
// ═════════════════════════════════════════════════════════════════════════════
let _searchTimer;
function onSearch(){ clearTimeout(_searchTimer); _searchTimer=setTimeout(()=>{ search=document.getElementById('search-input').value.toLowerCase(); applyFilters(); },160); }
function setCat(cat,btn){
  catFilter=cat;
  document.querySelectorAll('.chip').forEach(b=>b.classList.remove('is-active'));
  btn.classList.add('is-active');
  applyFilters();
}
function applyFilters(){
  page=1;
  filtered=allWallets.filter(w=>{
    let mc;
    if(catFilter==='smart'){
      mc = !isInfra(w) && ((w.smart_score ?? smartScore(w)) >= 55);
    } else if(catFilter==='no-infra'){
      mc = !isInfra(w);
    } else if(catFilter==='no-clusters'){
      mc = true; // dédup appliquée après le filter
    } else {
      mc = catFilter==='all' || (w.category||'Unknown')===catFilter;
    }
    const ms=!search||w.address.toLowerCase().includes(search)||(w.label||'').toLowerCase().includes(search);
    return mc&&ms;
  });
  // Cluster dedup post-filter : garde le top-volume par cluster_id
  if(catFilter==='no-clusters'){
    const bestByCluster = new Map();
    filtered = filtered.filter(w => {
      if(!w.cluster_id) return true;
      const cur = bestByCluster.get(w.cluster_id);
      const v = w.total_volume_usd || 0;
      if(!cur || (cur.total_volume_usd || 0) < v){
        if(cur){ const idx = filtered.indexOf(cur); /* dedup en passe finale ci-dessous */ }
        bestByCluster.set(w.cluster_id, w);
        return true;
      }
      return false;
    });
  }
  applySortRender();
}
function sortBy(field){
  if(sortField===field){sortAsc=!sortAsc;}else{sortField=field;sortAsc=false;}
  document.querySelectorAll('[id^="si-"]').forEach(el=>el.textContent='');
  const el=document.getElementById('si-'+field);
  if(el)el.textContent=sortAsc?'↑':'↓';
  applySortRender();
}
function updateFilterCounts(wallets){
  const counts={};
  wallets.forEach(w=>{ const c=w.category||'Unknown'; counts[c]=(counts[c]||0)+1; });
  const nonInfra = wallets.filter(w => !isInfra(w)).length;
  // Compte les clusters distincts pour le label "Dédup clusters"
  const clusters = new Set(wallets.filter(w => w.cluster_id).map(w => w.cluster_id));
  const dedupCount = wallets.filter(w => !w.cluster_id).length + clusters.size;
  const total=wallets.length;
  const labels={
    'all':            `Tous · ${total}`,
    'no-infra':       `Hors infra · ${nonInfra}`,
    'no-clusters':    `Dédup clusters · ${dedupCount}`,
    'MEV Bot':        `Prédateurs · ${counts['MEV Bot']||0}`,
    'DEX Protocol':   `DEX · ${counts['DEX Protocol']||0}`,
    'Market Maker':   `Market Maker · ${counts['Market Maker']||0}`,
    'Smart Contract': `Smart Contract · ${counts['Smart Contract']||0}`,
    'Unknown':        `Inconnu · ${counts['Unknown']||0}`
  };
  document.querySelectorAll('.chip').forEach(btn=>{
    const cat=btn.dataset.cat;
    if(labels[cat]) btn.textContent=labels[cat];
  });
}
function applySortRender(){
  _sorted=[...filtered].sort((a,b)=>{
    let va=a[sortField]??-Infinity, vb=b[sortField]??-Infinity;
    if(typeof va==='string')va=va.toLowerCase();
    if(typeof vb==='string')vb=vb.toLowerCase();
    return sortAsc?(va<vb?-1:va>vb?1:0):(va>vb?-1:va<vb?1:0);
  });
  renderTable(_sorted);
}
function changePage(d){
  page+=d;
  renderTable(_sorted);
  window.scrollTo({top:document.querySelector('#tbody').closest('.card').offsetTop-70,behavior:'smooth'});
}

// ═════════════════════════════════════════════════════════════════════════════
// Modal
// ═════════════════════════════════════════════════════════════════════════════
function openModal(addr){
  const w=allWallets.find(x=>x.address===addr);
  if(!w)return;
  const mev=w.mev_score||0;
  const mevBar=`<div class="flex items-center gap-1">${[0,1,2].map(i=>`<span style="width:8px;height:8px;border-radius:2px;background:${i<mev?'var(--c-mev)':'var(--border)'}"></span>`).join('')}<span class="text-muted" style="margin-left:6px">${['—','moyen','élevé'][mev]||'—'}</span></div>`;

  // Cluster info : trouve les autres wallets du même cluster pour les lister
  const siblings = (w.cluster_id ? allWallets.filter(x => x.cluster_id === w.cluster_id && x.address !== w.address) : []);
  const clusterRow = w.cluster_id
    ? `<div style="background:rgba(196,125,255,.06);border:1px solid rgba(196,125,255,.25);border-radius:8px;padding:10px 12px;margin:8px 0">
        <div style="font-size:11px;color:#dab5ff;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">
          ⛓ Cluster ${w.cluster_id} · ${w.cluster_size} wallets
        </div>
        <p class="fs-xs text-soft" style="margin:4px 0 6px">Ces wallets partagent le même deployer — probablement contrôlés par la même entité.</p>
        ${siblings.length ? '<div style="display:flex;flex-direction:column;gap:3px;margin-top:6px">' +
          siblings.slice(0, 5).map(s => `<a onclick="event.stopPropagation();closeModal();setTimeout(()=>openModal('${s.address}'),50)" style="cursor:pointer;color:#adc8ff;font-family:monospace;font-size:11px;text-decoration:underline">${truncAddr(s.address)} · ${formatUSDShort(s.total_volume_usd||0)} · score ${s.smart_score ?? '—'}</a>`).join('') +
          (siblings.length > 5 ? `<span class="fs-xs text-muted">+ ${siblings.length - 5} autres</span>` : '') +
          '</div>' : ''}
      </div>`
    : '';

  const rows=[
    ['Adresse',`<span class="mono fs-xs" style="color:var(--blue);word-break:break-all">${w.address}</span>`],
    ['Type',w.is_contract===true?'<span class="badge badge--sc">SMART CONTRACT</span>':w.is_contract===false?'<span class="badge badge--eoa">EOA</span>':'<span class="text-muted">—</span>'],
    ['Nom contrat',w.contract_name?`<span class="text-soft">${w.contract_name}</span>`:'<span class="text-muted">non vérifié</span>'],
    ['Label',w.label&&w.label!=='Unknown'?`<span class="text-soft font-semibold">${w.label}</span>`:'<span class="text-muted">Unknown</span>'],
    ['Catégorie',w.category?`<span class="${catClass(w.category)}">${w.category}</span>`:'—'],
    ['Score MEV',mevBar],
    ...(w.cluster_id ? [['__cluster__', clusterRow]] : []),
    ['',''],
    ['Volume Total',`<span class="text-green font-semibold">${formatUSD(w.total_volume_usd)}</span>`],
    ['Volume Dune DEX',formatUSD(w.dune_volume_usd)],
    ['Trades (7j)',fmtInt(w.dune_nb_trades)],
    ['Taille moy. trade',w.avg_trade_usd>0?`<span class="text-cyan">${formatUSD(w.avg_trade_usd)}</span>`:'—'],
    ['Tx Count',w.total_tx_count?fmtInt(w.total_tx_count):'—'],
    ['Token Transfers',fmtInt(w.token_transfer_count)],
    ['Tokens uniques',fmtInt(w.unique_tokens_traded)],
    ['Volume ETH récent',w.volume_eth_recent?w.volume_eth_recent.toFixed(4)+' ETH':'—'],
    ['Balance ETH',w.current_balance_eth!=null&&w.current_balance_eth>0?`<span class="text-cyan font-mono">${w.current_balance_eth.toFixed(4)} ETH</span>`:'—'],
    ['Gas dépensé',w.gas_spent_eth&&w.gas_spent_eth>0?w.gas_spent_eth.toFixed(6)+' ETH':'—'],
  ];

  document.getElementById('modal-body').innerHTML=`
    <div class="flex items-start justify-between mb-5">
      <div>
        <h3 class="font-bold" style="color:#fff;font-size:var(--fs-md)">Wallet #${w.rank}</h3>
        <p class="fs-xs text-muted mono mt-3">${truncAddr(w.address)}</p>
      </div>
      <button onclick="closeModal()" class="icon-btn" aria-label="Fermer">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="flex flex-col gap-2">
      ${rows.map(([k,v])=>{
        if(k === '__cluster__') return v; // pleine largeur, sans label
        if(!k) return '<div style="height:8px"></div>';
        return `<div class="flex items-start justify-between gap-3" style="padding:4px 0;border-bottom:1px solid var(--border)">
          <span class="fs-xs text-muted" style="width:120px;flex-shrink:0">${k}</span>
          <span class="fs-xs text-soft text-right">${v??'—'}</span>
        </div>`;
      }).join('')}
    </div>
    <div class="grid-3 mt-5">
      <a href="${currentExplorer()}/address/${w.address}" target="_blank" class="btn btn-ghost btn-sm" style="text-decoration:none">Etherscan ↗</a>
      <a href="https://debank.com/profile/${w.address}" target="_blank" class="btn btn-ghost btn-sm" style="text-decoration:none">DeBank ↗</a>
      <a href="https://app.zerion.io/${w.address}" target="_blank" class="btn btn-ghost btn-sm" style="text-decoration:none">Zerion ↗</a>
    </div>
    <div class="mt-4">
      <button id="btn-load-trades" onclick="loadWalletTrades('${w.address}')" class="btn btn-ghost" style="width:100%;height:36px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
        Charger résumé trades (7j via Dune)
      </button>
      <div id="trades-panel" class="mt-3 hidden"></div>
    </div>`;
  document.getElementById('modal').classList.remove('is-hidden');
}
function closeModal(){ document.getElementById('modal').classList.add('is-hidden'); }
document.getElementById('modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

async function loadWalletTrades(addr){
  const btn=document.getElementById('btn-load-trades');
  const panel=document.getElementById('trades-panel');
  btn.disabled=true;
  btn.innerHTML=`<svg class="spin" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10" stroke-dasharray="30 10" opacity=".3"/><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"/></svg> Interrogation Dune…`;
  panel.classList.add('hidden');
  panel.innerHTML='';
  try{
    const r=await fetch(`/api/wallet/${addr}/trades?chain=${encodeURIComponent(_currentChain)}`);
    const d=await r.json();
    if(d.error){panel.innerHTML=`<p class="fs-xs" style="color:var(--c-mev)">${d.error}</p>`;panel.classList.remove('hidden');return;}
    panel.innerHTML=renderTradesPanel(d);
    panel.classList.remove('hidden');
    btn.classList.add('hidden');
  }catch(e){
    panel.innerHTML=`<p class="fs-xs" style="color:var(--c-mev)">Erreur: ${e.message}</p>`;
    panel.classList.remove('hidden');
    btn.disabled=false;
    btn.innerHTML='Réessayer';
  }
}

function renderTradesPanel(d){
  const fmt=formatUSD; const fmtN=fmtNum;
  const fmtAmt=(n,sym)=>{
    const f=parseFloat(n);
    if(!f)return `— ${sym}`;
    return f>=1e6?`${(f/1e6).toFixed(2)}M ${sym}`:f>=1e3?`${(f/1e3).toFixed(2)}K ${sym}`:`${f.toFixed(4)} ${sym}`;
  };

  const statsHtml=`
    <div class="grid-3 mb-4">
      <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(46,212,168,.06);border:1px solid rgba(46,212,168,.18)">
        <p class="fs-xs text-muted mb-3">Volume ${d.days}j</p>
        <p class="font-bold text-green fs-sm">${fmt(d.total_volume_usd)}</p>
      </div>
      <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(59,166,255,.06);border:1px solid rgba(59,166,255,.18)">
        <p class="fs-xs text-muted mb-3">Trades</p>
        <p class="font-bold fs-sm" style="color:var(--blue)">${fmtN(d.total_trades)}</p>
      </div>
      <div style="padding:var(--s-3);border-radius:var(--radius-md);background:rgba(182,117,238,.06);border:1px solid rgba(182,117,238,.18)">
        <p class="fs-xs text-muted mb-3">Avg/trade</p>
        <p class="font-bold fs-sm" style="color:var(--c-mm)">${fmt(d.avg_trade_usd)}</p>
      </div>
    </div>`;

  const topPairsHtml=d.top_pairs.length?`
    <div class="mb-4">
      <p class="fs-xs uppercase tracking-wide text-muted mb-3 font-semibold">Top paires</p>
      <div class="flex flex-col gap-1">
        ${d.top_pairs.slice(0,6).map((p,i)=>{
          const maxV=d.top_pairs[0].volume_usd||1;
          const pct=Math.round((p.volume_usd/maxV)*100);
          return `<div class="flex items-center gap-2 fs-xs">
            <span class="text-muted" style="width:14px;flex-shrink:0">${i+1}</span>
            <div style="flex:1;min-width:0">
              <div class="flex items-center justify-between gap-2 mb-3">
                <span class="text-soft font-mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.pair}</span>
                <span class="text-green" style="flex-shrink:0">${fmt(p.volume_usd)}</span>
              </div>
              <div class="flex items-center gap-2">
                <div class="dist-bar" style="flex:1;height:3px"><div class="dist-bar-fill" style="width:${pct}%;background:var(--green)"></div></div>
                <span class="text-muted" style="flex-shrink:0">${fmtN(p.nb_trades)} trades · ${p.project}</span>
              </div>
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>`:'';

  const dexHtml=d.top_dexes.length?`
    <div class="mb-4">
      <p class="fs-xs uppercase tracking-wide text-muted mb-3 font-semibold">DEX utilisés</p>
      <div class="flex flex-wrap gap-2">
        ${d.top_dexes.map(x=>`<span class="chip">${x.project} <span class="text-cyan">${fmt(x.volume_usd)}</span></span>`).join('')}
      </div>
    </div>`:'';

  const recentHtml=d.recent_trades.length?`
    <div>
      <p class="fs-xs uppercase tracking-wide text-muted mb-3 font-semibold">Derniers trades</p>
      <div style="overflow-x:auto">
        <table class="tbl" style="font-size:11px">
          <thead><tr>
            <th>Heure</th><th>DEX</th><th>Acheté</th><th>Vendu</th><th class="text-right">USD</th><th class="text-center">Tx</th>
          </tr></thead>
          <tbody>
            ${d.recent_trades.slice(0,15).map(t=>`
              <tr>
                <td class="text-muted mono">${(t.time||'').slice(5,16)}</td>
                <td style="color:var(--blue)">${t.project}</td>
                <td class="text-green mono">${fmtAmt(t.bought_amount,t.bought_symbol)}</td>
                <td class="text-red mono">${fmtAmt(t.sold_amount,t.sold_symbol)}</td>
                <td class="text-right text-soft mono">${fmt(t.amount_usd)}</td>
                <td class="text-center">
                  <a href="${currentExplorer()}/tx/${t.tx_hash}" target="_blank" class="text-muted" style="text-decoration:none">↗</a>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`:'';

  return `<div style="padding-top:var(--s-4);border-top:1px solid var(--border)">
    <p class="fs-xs uppercase tracking-wide text-muted mb-3 font-semibold">Résumé trades · ${d.days}j · ${d.chains.join(', ')||'ethereum'}</p>
    ${statsHtml}${topPairsHtml}${dexHtml}${recentHtml}
  </div>`;
}

// ═════════════════════════════════════════════════════════════════════════════
// Data
// ═════════════════════════════════════════════════════════════════════════════
// État du dashboard : 'loading' | 'success' | 'empty' | 'error'
// — initial : 'loading' tant que le 1er fetch n'a pas répondu
let _dashState = 'loading';

function _renderTbodyState(state, msg){
  const tbody = document.getElementById('tbody');
  if(!tbody) return;
  if(state === 'empty'){
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12c2-2 4-2 6 0s4 2 6 0 4-2 6 0 4 2 6 0"/><path d="M2 18c2-2 4-2 6 0s4 2 6 0 4-2 6 0 4 2 6 0"/></svg>
      <p>${msg || 'Aucun wallet à afficher pour le moment.'}</p>
      <p class="fs-xs text-muted mt-3">Une nouvelle analyse sera lancée automatiquement par le pipeline.</p>
    </td></tr>`;
  } else if(state === 'error'){
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state empty-state--error">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      <p>${msg || 'Erreur de chargement des données.'}</p>
      <button onclick="loadData()" class="btn btn-primary btn-sm">Réessayer</button>
    </td></tr>`;
  }
}

function _showKpiSkeletons(){
  const ids = ['stat-volume','stat-total','stat-mev','hero-wallets','hero-volume','hero-mev'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if(el && !el.querySelector('.skel')){
      el.innerHTML = '<span class="skel skel--num-l">—</span>';
    }
  });
}

// ═════════════════════════════════════════════════════════════════════════════
// Multi-chain — état + helpers
// ═════════════════════════════════════════════════════════════════════════════
let _currentChain = (() => {
  // Priorité : ?chain= dans l'URL > localStorage > ethereum par défaut.
  // Permet aux liens externes (/?chain=arbitrum) de pre-sélectionner une chain.
  try {
    const qs = new URLSearchParams(location.search);
    const qp = qs.get('chain');
    const valid = ['ethereum','arbitrum','base','optimism','polygon','bnb','avalanche'];
    if(qp && valid.includes(qp)){
      try { localStorage.setItem('ww_chain', qp); } catch(e){}
      return qp;
    }
    return localStorage.getItem('ww_chain') || 'ethereum';
  } catch(e){ return 'ethereum'; }
})();
const CHAIN_EXPLORERS = {
  ethereum:  'https://etherscan.io',
  arbitrum:  'https://arbiscan.io',
  base:      'https://basescan.org',
  optimism:  'https://optimistic.etherscan.io',
  polygon:   'https://polygonscan.com',
  bnb:       'https://bscscan.com',
  avalanche: 'https://snowtrace.io',
};
// Couleurs officielles inspirées des marques de chaque chain — utilisées
// pour le dot/glow du chain selector et la live-chip
const CHAIN_ACCENT = {
  ethereum:  { color: '#00e2ff',  label: 'ETH'  },  // cyan (ETH brand)
  arbitrum:  { color: '#28a0f0',  label: 'ARB'  },  // Arbitrum blue
  base:      { color: '#0052ff',  label: 'BASE' },  // Coinbase blue
  optimism:  { color: '#ff0420',  label: 'OP'   },  // Optimism red
  polygon:   { color: '#8247e5',  label: 'POL'  },  // Polygon purple
  bnb:       { color: '#f0b90b',  label: 'BNB'  },  // Binance yellow
  avalanche: { color: '#e84142',  label: 'AVAX' },  // Avalanche red
};
function currentExplorer(){ return CHAIN_EXPLORERS[_currentChain] || CHAIN_EXPLORERS.ethereum; }
function currentChainAccent(){ return CHAIN_ACCENT[_currentChain] || CHAIN_ACCENT.ethereum; }
function _applyChainTheme(chain){
  const a = CHAIN_ACCENT[chain] || CHAIN_ACCENT.ethereum;
  const dot = document.getElementById('chain-dot');
  if(dot){
    dot.style.background = a.color;
    dot.style.boxShadow = `0 0 10px ${a.color}`;
  }
  const chip = document.getElementById('live-chip');
  if(chip){
    chip.dataset.chain = chain;
    chip.textContent = `LIVE · 7j · ${a.label}`;
    chip.style.borderColor = a.color;
    chip.style.color = a.color;
    // Background avec opacité faible de la couleur d'accent
    chip.style.background = a.color + '15'; // 15 = ~8% en hex
  }
  // Met à jour le <title> de l'onglet — utile pour la nav multi-onglets
  // Format : "WhaleWatch · Arbitrum — ETH On-Chain Radar"
  const chainName = chain.charAt(0).toUpperCase() + chain.slice(1);
  document.title = `WhaleWatch · ${chainName} — On-Chain Radar`;
  // Met à jour les sous-titres KPI qui mentionnent la chain
  const sub = document.getElementById('stat-volume-sub');
  const labels = {ethereum:'Ethereum', arbitrum:'Arbitrum', base:'Base',
                  optimism:'Optimism', polygon:'Polygon', bnb:'BNB Chain',
                  avalanche:'Avalanche'};
  if(sub) sub.textContent = `Volume DEX ${labels[chain] || chainName}`;
  // Note : btn-refresh-label retiré avec le bouton Sonar
}
async function loadChainOverview(){
  // Petit widget qui montre les 6 chains avec leurs volumes — clic = switch.
  const grid = document.getElementById('chain-overview-grid');
  if(!grid) return;
  try{
    const r = await fetch('/api/chains/summary');
    if(!r.ok) return;
    const d = await r.json();
    const chains = d.chains || [];
    const fmtV = v => v >= 1e9 ? '$'+(v/1e9).toFixed(2)+'B' : v >= 1e6 ? '$'+(v/1e6).toFixed(0)+'M' : v>0 ? '$'+Math.round(v).toLocaleString() : '—';
    const STALE_THRESHOLD_MS = 24 * 3600 * 1000; // 24h
    grid.innerHTML = chains.map(c => {
      const a = CHAIN_ACCENT[c.key] || CHAIN_ACCENT.ethereum;
      const isActive = c.key === _currentChain;
      const ageMs = c.last_updated ? (Date.now() - new Date(c.last_updated).getTime()) : null;
      const isStale = ageMs != null && ageMs > STALE_THRESHOLD_MS;
      const age = ageMs != null ? _humanAge(ageMs) : 'pas encore d\'analyse';
      const staleIcon = isStale ? '<span title="Cache > 24h" style="color:#ffb050;margin-left:4px">⏱</span>' : '';
      return `<button onclick="onChainChange('${c.key}')" type="button"
        class="chain-mini-card"
        aria-pressed="${isActive}"
        style="text-align:left;border:1px solid ${isActive ? a.color : 'var(--border)'};background:${isActive ? a.color + '12' : 'rgba(2,12,27,.55)'};border-radius:8px;padding:10px 12px;cursor:pointer;transition:all .15s ease;display:flex;flex-direction:column;gap:4px;color:inherit;font:inherit;height:76px;box-sizing:border-box${isStale ? ';opacity:.75' : ''}">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
          <div style="display:flex;align-items:center;gap:6px">
            <span style="width:8px;height:8px;border-radius:50%;background:${a.color};box-shadow:0 0 6px ${a.color}"></span>
            <span style="font-weight:700;font-size:11px;letter-spacing:.04em">${c.label}</span>
          </div>
          ${isActive ? `<span style="font-size:9px;color:${a.color};font-weight:700">ACTIVE</span>` : ''}
        </div>
        <div style="font-family:var(--font-mono);font-weight:700;font-size:16px;color:${c.has_data ? 'var(--text)' : 'var(--muted)'}">${fmtV(c.total_volume_usd)}</div>
        <div class="fs-xs text-muted" style="font-size:10px">${c.total_wallets || 0} wallets · ${age}${staleIcon}</div>
      </button>`;
    }).join('');
  }catch(e){ /* silencieux : l'utilisateur n'en sera pas privé du dashboard */ }
}

function onChainChange(chain){
  if(chain === _currentChain) return;
  _currentChain = chain;
  try { localStorage.setItem('ww_chain', chain); } catch(e){}
  _applyChainTheme(chain);
  // Reset state + reload everything
  _whaleAlertShown = false;
  _lastUpdatedAt = null;  // évite d'afficher l'âge de l'ancienne chain
  _showKpiSkeletons();
  const tbody = document.getElementById('tbody');
  if(tbody) tbody.innerHTML = '<tr><td colspan="8"><span class="skel skel--row" style="width:96%"></span></td></tr><tr><td colspan="8"><span class="skel skel--row" style="width:92%"></span></td></tr>';
  loadData();
  // Re-évalue le panel alertes (caché si chain ≠ ethereum) et redessine
  // le widget multi-chain (pour mettre à jour l'active card)
  if(typeof loadAlerts === 'function') loadAlerts();
  if(typeof loadChainOverview === 'function') loadChainOverview();
  // Sync le selector dropdown si onChainChange déclenché autrement que par lui
  const sel = document.getElementById('chain-selector');
  if(sel && sel.value !== chain) sel.value = chain;
  const a = CHAIN_ACCENT[chain] || CHAIN_ACCENT.ethereum;
  showToast(`Bascule sur ${chain.charAt(0).toUpperCase() + chain.slice(1)} (${a.label})`, 'success', 1800);
}

async function loadData(){
  // Si on est en error/empty et que l'utilisateur clique "Réessayer", remettre des skeletons
  if(_dashState === 'error' || _dashState === 'empty'){
    _showKpiSkeletons();
    const tbody = document.getElementById('tbody');
    if(tbody) tbody.innerHTML = '<tr><td colspan="8"><span class="skel skel--row" style="width:96%"></span></td></tr><tr><td colspan="8"><span class="skel skel--row" style="width:92%"></span></td></tr><tr><td colspan="8"><span class="skel skel--row" style="width:94%"></span></td></tr>';
  }
  _dashState = 'loading';
  try{
    const r = await fetch(`/api/wallets?chain=${encodeURIComponent(_currentChain)}`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const data = await r.json();
    allWallets = (data.wallets||[]).map(w => {
      const avg = (w.dune_nb_trades && w.dune_nb_trades > 0) ? (w.dune_volume_usd||0)/w.dune_nb_trades : 0;
      const enriched = {...w, avg_trade_usd: avg};
      if(enriched.smart_score == null) enriched.smart_score = smartScoreLocal(enriched);
      return enriched;
    });
    if(allWallets.length){
      _dashState = 'success';
      maxVol = Math.max(...allWallets.map(w => w.total_volume_usd||0));
      updateStats(data);
      updateCharts(allWallets);
      renderSmartLeaderboard(allWallets);
      applyFilters();
      updateFilterCounts(allWallets);
      renderSentimentBadge(computeSentiment(allWallets));
      if(!_whaleAlertShown){
        _whaleAlertShown = true;
        const topW = allWallets[0];
        const topVol = topW?.total_volume_usd || topW?.dune_volume_usd || 0;
        if(topVol > 100_000_000){
          const addr = topW.address.slice(0,6)+'…'+topW.address.slice(-4);
          const trades = (topW.dune_nb_trades||topW.trades||0).toLocaleString('en-US');
          const fmtV = v => v>=1e9 ? '$'+(v/1e9).toFixed(2)+'B' : v>=1e6 ? '$'+(v/1e6).toFixed(0)+'M' : '$'+Math.round(v).toLocaleString();
          const catEmoji = topW.category==='MEV Bot' ? '🦈' : topW.category==='Market Maker' ? '🐬' : '🐋';
          showToast(`${catEmoji} Baleine détectée · ${addr} · ${fmtV(topVol)} · ${trades} trades/7j`, 'whale', 5200);
        }
      }
    } else {
      // Réponse OK mais 0 wallet → vrai vide. Affiche le bouton « Lancer une analyse ».
      _dashState = 'empty';
      ['stat-volume','stat-total','stat-mev'].forEach(id => { const el=document.getElementById(id); if(el) el.textContent = '—'; });
      ['hero-wallets','hero-volume','hero-mev'].forEach(id => { const el=document.getElementById(id); if(el){ el.classList.remove('skel','skel--text','skel--num-l'); el.textContent = '—'; } });
      _renderTbodyState('empty', 'Pas encore de données — lance une analyse pour scanner l\'océan.');
    }
  } catch(e){
    console.error('[loadData]', e);
    _dashState = 'error';
    ['stat-volume','stat-total','stat-mev'].forEach(id => { const el=document.getElementById(id); if(el) el.textContent = '—'; });
    ['hero-wallets','hero-volume','hero-mev'].forEach(id => { const el=document.getElementById(id); if(el){ el.classList.remove('skel','skel--text','skel--num-l'); el.textContent = '—'; } });
    _renderTbodyState('error', 'Impossible de charger les wallets — vérifie ta connexion ou réessaie.');
  }
}

// Note : le bouton Sonar a été retiré du UI (sonars lancés par cron côté
// serveur). Les fonctions triggerRefresh / _getRefreshToken /
// _promptRefreshToken ont été supprimées. Le polling patterns reste actif
// pour récupérer les patterns à la prochaine analyse complétée.

// ─── Polling patterns indépendant du polling wallets ────────────────────────
let _patternsPoll = null;
function _pollPatternsUntilReady(){
  if(_patternsPoll) clearInterval(_patternsPoll);
  let tries = 0, maxTries = WW_CONFIG.PATTERNS_POLL_MAX;
  // myId : capture locale pour détecter une obsolescence (un nouveau poll a remplacé celui-ci).
  // Sans ça, un poll en plein await peut accidentellement clearInterval le nouveau poll.
  let myId;
  myId = _patternsPoll = setInterval(async () => {
    if(myId !== _patternsPoll) return;                       // remplacé par un poll plus récent
    if(++tries > maxTries){
      clearInterval(myId); if(myId === _patternsPoll) _patternsPoll = null;
      // Feedback utilisateur sur échec timeout (silent return = bug #3)
      const st = await fetch('/api/status').then(r=>r.json()).catch(()=>null);
      const msg = st?.error
        ? `Patterns : ${st.error}`
        : 'Patterns whales : calcul trop long — réessaie via Sonar';
      showToast(msg, 'error', 4200);
      return;
    }
    try{
      const r = await fetch(`/api/patterns?chain=${encodeURIComponent(_currentChain)}`);
      if(myId !== _patternsPoll) return;                     // remplacé pendant le await
      if(!r.ok) return;
      const d = await r.json();
      if(myId !== _patternsPoll) return;                     // idem
      if(!d || d.error) return;
      renderPatterns(d);
      _setPatternsMeta(d);
      _resetPatternsBtn();
      clearInterval(myId); if(myId === _patternsPoll) _patternsPoll = null;
      showToast('Patterns whales mis à jour — Zones d\'entrée/sortie actives', 'success');
    }catch(e){}
  }, WW_CONFIG.PATTERNS_POLL_MS);
}

function startPoll(){
  if(poll)clearInterval(poll);
  document.getElementById('status-bar').classList.remove('hidden');
  document.getElementById('progress-line').classList.remove('hidden');
  poll=setInterval(doPoll, WW_CONFIG.STATUS_POLL_MS);
}

async function doPoll(){
  // Poll discret du status — affiche la barre si un Sonar tourne côté
  // serveur (lancé par le cron). Le bouton Sonar UI est retiré, mais
  // l'utilisateur peut voir qu'une analyse est en cours.
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    const textEl = document.getElementById('status-text');
    if(textEl) textEl.textContent = d.progress || d.status || 'En cours…';
    if(d.status==='completed'){
      clearInterval(poll); poll=null;
      document.getElementById('status-bar')?.classList.add('hidden');
      document.getElementById('progress-line')?.classList.add('hidden');
      await loadData();
      showToast('Analyse terminée — données mises à jour','success');
    }else if(d.status==='error'){
      clearInterval(poll); poll=null;
      document.getElementById('status-bar')?.classList.add('hidden');
      document.getElementById('progress-line')?.classList.add('hidden');
      if(textEl) textEl.textContent = 'Erreur · ' + d.error;
    }
  }catch(e){}
}

// ═════════════════════════════════════════════════════════════════════════════
// CSV export
// ═════════════════════════════════════════════════════════════════════════════
function exportCSV(){
  if(!allWallets.length)return;
  // Inclut les nouvelles colonnes : smart_score, smart_label, cluster_id
  const cols=['rank','address','label','category','is_contract','total_volume_usd','dune_volume_usd','dune_nb_trades','smart_score','smart_label','cluster_id','cluster_size','total_tx_count','token_transfer_count','current_balance_eth','gas_spent_eth','mev_score'];
  const csv=[cols.join(','),...allWallets.map(w=>cols.map(c=>{const v=w[c]??'';return typeof v==='string'&&v.includes(',')?`"${v}"`:v;}).join(','))].join('\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  // Filename reflète la chain active (ex: top_wallets_arbitrum_2026-06-05.csv)
  a.download=`top_wallets_${_currentChain}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
}

// ═════════════════════════════════════════════════════════════════════════════
// Helpers
// ═════════════════════════════════════════════════════════════════════════════
function truncAddr(a){return a?a.slice(0,6)+'…'+a.slice(-4):'—';}
function trunc(s,n){return s&&s.length>n?s.slice(0,n)+'…':s||'—';}
function fmtInt(v){if(v==null||v===0)return'—';return Math.round(v).toLocaleString('en-US');}
const fmtNum   = v => Number(v||0).toLocaleString('en-US');
const fmtPrice = v => v ? '$'+Math.round(v).toLocaleString('en-US') : '—';
const fmtP     = v => '$' + Math.round(v).toLocaleString('en-US');
const fmtP2    = v => '$' + Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtPct   = (v,d=2) => (v>=0?'+':'') + Number(v).toFixed(d) + '%';
function formatUSD(v){
  if(v==null||v===0)return'$0';
  if(v>=1e9)return'$'+(v/1e9).toFixed(2)+'B';
  if(v>=1e6)return'$'+(v/1e6).toFixed(2)+'M';
  if(v>=1e3)return'$'+(v/1e3).toFixed(1)+'K';
  return'$'+v.toFixed(0);
}
function formatUSDShort(v){
  if(!v)return'$0';
  if(v>=1e12)return'$'+(v/1e12).toFixed(1)+'T';
  if(v>=1e9)return'$'+(v/1e9).toFixed(1)+'B';
  if(v>=1e6)return'$'+(v/1e6).toFixed(1)+'M';
  if(v>=1e3)return'$'+(v/1e3).toFixed(0)+'K';
  return'$'+v.toFixed(0);
}
function copyAddr(e,addr){
  e.stopPropagation();
  navigator.clipboard.writeText(addr).then(()=>showToast('Adresse copiée','success')).catch(()=>showToast('Adresse copiée','success'));
}

// ═════════════════════════════════════════════════════════════════════════════
// Boot
// ═════════════════════════════════════════════════════════════════════════════
// ─── Boot — guarded pour soft-nav (évite l'accumulation de setIntervals) ───
// La 1ère fois : initialise charts, timers, fetch initial.
// Les soft-nav retours sur / : ne RE-init pas (timers déjà actifs depuis app.js)
// mais reload juste les données pour rafraîchir l'affichage.
if (!window.__wwBodyInit) {
  window.__wwBodyInit = true;
  applyProMode();
  // Restore le sélecteur de chain depuis localStorage
  const sel = document.getElementById('chain-selector');
  if(sel) sel.value = _currentChain;
  // Applique le thème de chain (dot color + live-chip) au boot
  _applyChainTheme(_currentChain);
  // Populate dynamiquement depuis /api/chains (au cas où on ajoute une chain server-side)
  fetch('/api/chains').then(r => r.ok ? r.json() : null).then(chains => {
    if(!chains || !chains.length || !sel) return;
    const current = sel.value;
    sel.innerHTML = chains.map(c => `<option value="${c.key}">${c.label}</option>`).join('');
    sel.value = current;
  }).catch(()=>{});
  // ─── Lazy-load Chart.js ─────────────────────────────────────────────
  // Chart.js (~228 KiB minified) n'est plus chargé au boot. Il est
  // injecté à la demande quand le user scrolle vers la section des charts,
  // ou en fallback après 3s d'inactivité (requestIdleCallback).
  let _chartJsPromise = null;
  function _loadChartJsLazy(){
    if (_chartJsPromise) return _chartJsPromise;
    if (typeof Chart !== 'undefined') return _chartJsPromise = Promise.resolve();
    _chartJsPromise = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
      s.async = true;
      s.onload  = () => { initCharts(); if (allWallets && allWallets.length) updateCharts(allWallets); resolve(); };
      s.onerror = () => { console.warn('[ChartJS] load failed'); resolve(); };
      document.head.appendChild(s);
    });
    return _chartJsPromise;
  }
  function _initChartsLazyObserver(){
    const target = document.getElementById('chartBar')?.closest('.card');
    if (!target || !('IntersectionObserver' in window)) {
      // Fallback : charge après 3s ou idle
      const fallback = () => _loadChartJsLazy();
      if ('requestIdleCallback' in window) requestIdleCallback(fallback, { timeout: 3000 });
      else setTimeout(fallback, 3000);
      return;
    }
    const io = new IntersectionObserver((entries, obs) => {
      if (entries.some(e => e.isIntersecting)) {
        _loadChartJsLazy();
        obs.disconnect();
      }
    }, { rootMargin: '200px' });
    io.observe(target);
  }
  // loadData() est appelé immédiatement — les charts s'updaterent à la
  // 2ème occurrence (via le observer ou le fallback idle).
  loadData();
  loadChainOverview();
  _initChartsLazyObserver();
  fetch('/api/status').then(r=>r.json()).then(d=>{ if(d.status==='running') startPoll(); });
  fetch(`/api/patterns?chain=${encodeURIComponent(_currentChain)}`).then(r=>r.ok?r.json():null).then(d=>{
    if(d && !d.error){
      renderPatterns(d); _setPatternsMeta(d); _resetPatternsBtn();
    } else {
      _pollPatternsUntilReady();
    }
  }).catch(()=>{ _pollPatternsUntilReady(); });
  _startLiveCounter();
  _startPriceRefresh();
  // _scheduleAutoRefresh retiré avec le bouton Sonar (cron côté serveur)
  _startAlertsPolling();
  _startCryptoTicker();
} else {
  // Soft-nav re-entry : <main> a été swappé donc :
  //  - les <canvas> sont neufs → rebinder les charts (anciens references = stale)
  //  - le tbody/patterns-body sont vides → re-render
  //  - mais on NE recrée PAS les setInterval/setTimeout (déjà actifs en global)
  applyProMode();
  // Si Chart.js était déjà chargé (re-entry après visite charts), on re-init ;
  // sinon on attendra que le lazy-load déclenche initCharts() à son tour.
  if (typeof Chart !== 'undefined') initCharts();
  loadData();
  if (window._lastPatterns) {
    renderPatterns(window._lastPatterns);
    _setPatternsMeta(window._lastPatterns);
    _resetPatternsBtn();
  }
}