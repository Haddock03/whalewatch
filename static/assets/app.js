/* ════════════════════════════════════════════════════════════════════════
   WhaleWatch — Shell App
   ──────────────────────────────────────────────────────────────────────
   1. SOFT NAVIGATION : intercepte les clics sur les liens internes,
      fetch le HTML cible, remplace UNIQUEMENT le <main>. Le <header>
      n'est jamais re-rendu — visuellement fixe à travers les pages.
   2. AUTO-REFRESH    : recharge périodiquement /api/wallets et
      /api/patterns pour que la donnée du dashboard reste à jour
      sans intervention utilisateur.
═══════════════════════════════════════════════════════════════════════ */
(function(){
  'use strict';

  // ── Soft navigation ───────────────────────────────────────────────────
  const ROUTES = new Set(['/', '/pro/cockpit', '/pro/hot',
                          '/pro/backtest', '/pro/watchlist', '/pro/guide',
                          '/why', '/guide', '/bot',
                          '/index.html', '/why.html',
                          '/guide.html', '/bot.html']);
  let _navigating = false;

  // Tag les <style> inline initiaux pour qu'ils soient remplacés à la
  // 1ère navigation (sinon on se retrouve avec les styles du dashboard
  // ET ceux de /why simultanément).
  // NB : snapshot au boot — si une lib (Chart.js etc.) injecte un <style>
  // runtime APRÈS app.js, il ne sera pas tagué donc préservé. Pour opt-out
  // explicite, ajouter `data-keep` sur le <style> côté HTML.
  document.head.querySelectorAll('style:not([data-keep])').forEach(s =>
    s.setAttribute('data-page-style', '')
  );

  async function softNavigate(href, push = true) {
    if (_navigating) return;
    if (!ROUTES.has(href)) { location.href = href; return; }
    _navigating = true;

    // Subtle loading hint sur le main
    const oldMain = document.querySelector('main');
    if (oldMain) oldMain.style.opacity = '0.45';

    try {
      const r = await fetch(href, { headers: { 'X-Soft-Nav': '1' } });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const html = await r.text();
      const doc  = new DOMParser().parseFromString(html, 'text/html');
      const newMain = doc.querySelector('main');
      if (!newMain || !oldMain) { location.href = href; return; }

      // Swap function (utilisée avec ou sans view-transition)
      const swap = () => {
        document.title = doc.title || document.title;

        // 1. Remplace les <style> page-spécifiques :
        // chaque page a son <style> inline dans <head> qui définit ses
        // composants (icônes, cards…). Sans ça, les SVGs s'affichent
        // à leur taille SVG par défaut (gros).
        document.head.querySelectorAll('style[data-page-style]').forEach(s => s.remove());
        const newStyles = doc.head.querySelectorAll('style');
        newStyles.forEach(orig => {
          const code = orig.textContent || '';
          if (!code.trim()) return;
          const s = document.createElement('style');
          s.setAttribute('data-page-style', '');
          s.textContent = code;
          document.head.appendChild(s);
        });

        // 2. Swap du <main>
        oldMain.replaceWith(newMain);
        newMain.style.opacity = '';

        // 3. Maj active state des nav-links
        document.querySelectorAll('.nav .nav-link').forEach(a => {
          const ah = a.getAttribute('href');
          a.classList.toggle('is-active', ah === href || (href === '/' && ah === '/'));
        });

        // 4. Nettoie les anciens scripts page-spécifiques avant d'en ajouter
        document.querySelectorAll('script[data-page-script]').forEach(s => s.remove());

        // 5. Re-exécute les <script> trouvés dans le nouveau <main>
        // ou en bas du body de la page cible.
        const scripts = [
          ...newMain.querySelectorAll('script'),
          ...Array.from(doc.body.querySelectorAll(':scope > script'))
        ];
        scripts.forEach(orig => {
          // Évite de ré-importer les scripts externes déjà chargés
          if (orig.src) {
            if ([...document.scripts].some(s => s.src === orig.src)) return;
            const s = document.createElement('script');
            s.setAttribute('data-page-script', '');
            s.src = orig.src; s.async = false;
            document.body.appendChild(s);
            return;
          }
          // Inline : wrap en IIFE pour isoler le scope
          // (évite "redeclaration of const X" entre pages)
          const code = orig.textContent || '';
          if (!code.trim()) return;
          const s = document.createElement('script');
          s.setAttribute('data-page-script', '');
          s.textContent = `(function(){\ntry{\n${code}\n}catch(e){console.warn('[soft-nav script]', e)}\n})();`;
          document.body.appendChild(s);
        });

        if (push) history.pushState({ href }, '', href);
        window.scrollTo(0, 0);
      };

      // View Transitions API si dispo → animation douce
      if (document.startViewTransition) {
        document.startViewTransition(swap);
      } else {
        swap();
      }
    } catch (e) {
      console.warn('[soft-nav] fallback hard nav:', e);
      location.href = href;
    } finally {
      _navigating = false;
    }
  }

  // Intercept clicks on internal links (anchor delegation)
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a');
    if (!a) return;
    const href = a.getAttribute('href');
    if (!href) return;
    if (a.target === '_blank' || a.hasAttribute('download')) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    if (!href.startsWith('/') || href.startsWith('//')) return;
    if (!ROUTES.has(href)) return;
    e.preventDefault();
    softNavigate(href);
  });

  // Back / forward
  window.addEventListener('popstate', () => softNavigate(location.pathname, false));

  // ── Auto-refresh des données dashboard ───────────────────────────────
  // Le dashboard expose loadData() et loadPatterns() via window.
  // Intervalles lus depuis WW_CONFIG (défini dans index.html, exposé global).
  // Fallbacks si app.js charge avant ou hors du dashboard.
  const CFG = window.WW_CONFIG || {};
  const WALLETS_INTERVAL_MS  = CFG.WALLETS_REFRESH_MS  || 60_000;
  const PATTERNS_INTERVAL_MS = CFG.PATTERNS_REFRESH_MS || 5 * 60_000;

  let _walletsTimer  = null;
  let _patternsTimer = null;
  let _autoRefreshOn = true;

  function _tickWallets() {
    if (document.hidden) return;            // pas de refresh quand onglet caché
    if (typeof window.loadData === 'function') {
      try { window.loadData(); } catch (e) { console.warn('[refresh] loadData', e); }
    }
  }
  function _tickPatterns() {
    if (document.hidden) return;
    if (typeof window.loadPatterns === 'function' && typeof window._lastPatterns !== 'undefined') {
      // On ne fetch que si on a déjà des patterns (sinon c'est au user de Sonarer)
      if (window._lastPatterns) {
        try { window.loadPatterns(); } catch (e) { console.warn('[refresh] loadPatterns', e); }
      }
    }
  }

  function startAutoRefresh() {
    stopAutoRefresh();
    _walletsTimer  = setInterval(_tickWallets,  WALLETS_INTERVAL_MS);
    _patternsTimer = setInterval(_tickPatterns, PATTERNS_INTERVAL_MS);
  }
  function stopAutoRefresh() {
    if (_walletsTimer)  clearInterval(_walletsTimer);
    if (_patternsTimer) clearInterval(_patternsTimer);
    _walletsTimer = _patternsTimer = null;
  }

  // Pause quand l'onglet est caché, reprend au focus
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (_autoRefreshOn) {
      _tickWallets();   // refresh immédiat au retour
    }
  });

  // ── Live ETH/BTC prices (CoinGecko) ──────────────────────────────────
  // Auto-fire si la page contient #eth-price ou #btc-price.
  // Utilisé par les pages Pro qui ont leur header simplifié sans le code
  // _setPriceEl du dashboard (qui fait + de choses, dont MEV refs).
  let _ethBtcTimer = null;
  const fmtUSD = v => v ? '$' + Math.round(v).toLocaleString('en-US') : '—';

  async function _fetchEthBtc() {
    const ethEl = document.getElementById('eth-price');
    const btcEl = document.getElementById('btc-price');
    if (!ethEl && !btcEl) return;
    try {
      const r = await fetch('/api/prices');
      if (!r.ok) return;
      const d = await r.json();
      // N'écrase pas le contenu si le dashboard (index.html) gère déjà via _setPriceEl
      if (ethEl && !window._prevPrices && d?.ethereum?.usd) ethEl.textContent = fmtUSD(d.ethereum.usd);
      if (btcEl && !window._prevPrices && d?.bitcoin ?.usd) btcEl.textContent = fmtUSD(d.bitcoin.usd);
    } catch {}
  }

  function startEthBtcPolling() {
    _fetchEthBtc();
    if (_ethBtcTimer) clearInterval(_ethBtcTimer);
    const ms = (CFG.PRICE_REFRESH_MS || 30_000);
    _ethBtcTimer = setInterval(_fetchEthBtc, ms);
  }
  startEthBtcPolling();

  // ── Horloge live UTC dans le header (#header-clock) ──────────────────
  function _tickClock() {
    const el = document.getElementById('header-clock');
    if (!el) return;
    const d = new Date();
    el.textContent = d.toISOString().slice(11, 19) + ' UTC';
  }
  setInterval(_tickClock, 1000); _tickClock();

  // Exposer pour debug + démarrer
  window.WhaleWatch = window.WhaleWatch || {};
  window.WhaleWatch.softNavigate     = softNavigate;
  window.WhaleWatch.startAutoRefresh = startAutoRefresh;
  window.WhaleWatch.stopAutoRefresh  = stopAutoRefresh;

  startAutoRefresh();
})();
