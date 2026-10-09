/* Gemeinsame Navigation aller App-Seiten. Neue Eintraege nur hier pflegen. */
(function () {
  const LINKS = [
    { href: '/projects#new', match: '/projects#new', label: 'Neues Vorhaben', primary: true, requires: '/projects' },
    { href: '/projects', match: '/projects', label: 'Meine Vorhaben', requires: '/projects' },
    { href: '/agent-dashboard', label: 'Freigaben', badge: true },
    { href: '/sourcing-dashboard', label: 'Dienstleister' },
    { href: '/rfq-dashboard', label: 'Angebote & Vertraege' },
    { href: '/cases-dashboard', label: 'Verhandlungen' },
    { href: '/data-center', label: 'Data Center' },
  ];
  const AVAILABLE = (window.NX_NAV_PAGES || ['/agent-dashboard', '/sourcing-dashboard', '/rfq-dashboard', '/cases-dashboard', '/data-center']);

  const css = `
  #app-nav{position:fixed;top:0;left:0;right:0;z-index:300;height:56px;background:rgba(247,246,241,.94);backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:14px;font-family:'DM Sans',sans-serif}
  #app-nav .nx-logo{font-family:'Playfair Display',serif;font-size:17px;font-weight:700;color:var(--text);text-decoration:none;flex-shrink:0}
  #app-nav .nx-logo em{color:var(--gold);font-style:normal}
  #app-nav .nx-links{display:flex;align-items:center;gap:4px;flex:1;min-width:0}
  #app-nav .nx-link{font-size:13px;color:var(--text-muted);text-decoration:none;padding:7px 10px;border-radius:7px;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
  #app-nav .nx-link:hover{color:var(--text);background:rgba(0,0,0,.035)}
  #app-nav .nx-link.on{color:var(--text);background:var(--bg-card);box-shadow:inset 0 0 0 1px var(--border)}
  #app-nav .nx-link.primary{color:#fff;background:var(--gold)}
  #app-nav .nx-link.primary:hover{background:var(--gold-light)}
  #app-nav .nx-badge{font-family:'DM Mono',monospace;font-size:10.5px;background:var(--gold);color:#fff;border-radius:9px;padding:1px 7px;line-height:16px}
  #app-nav .nx-right{display:flex;align-items:center;gap:8px;margin-left:auto;flex-shrink:0}
  #app-nav .nx-btn{font-size:12px;color:var(--text-muted);background:transparent;border:1px solid var(--border);padding:5px 11px;border-radius:6px;cursor:pointer;text-decoration:none;font-family:inherit}
  #app-nav .nx-btn:hover{color:var(--text);border-color:var(--gold-border)}
  #app-nav .nx-user{font-size:12px;color:var(--text-dim);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #app-nav .nx-menu{display:none}
  @media(max-width:1100px){
    #app-nav .nx-links{display:none;position:absolute;top:56px;left:0;right:0;flex-direction:column;align-items:stretch;background:var(--bg);
      border-bottom:1px solid var(--border);padding:10px 16px;gap:2px;box-shadow:0 12px 24px rgba(0,0,0,.06)}
    #app-nav.open .nx-links{display:flex}
    #app-nav .nx-link{padding:10px 12px}
    #app-nav .nx-menu{display:inline-block}
    #app-nav .nx-user{display:none}
  }`;

  function esc(s) { return (s ?? '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  function render() {
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    let user = null;
    try { user = JSON.parse(localStorage.getItem('nx_user') || 'null'); } catch (e) { user = null; }
    const here = location.pathname + location.hash;
    const links = LINKS.filter(l => !l.requires || AVAILABLE.includes(l.requires));
    const isOn = l => {
      if (l.match === '/projects#new') return location.pathname === '/projects' && location.hash === '#new';
      if (l.href === '/projects') return location.pathname === '/projects' && location.hash !== '#new';
      return location.pathname === l.href;
    };

    let header = document.getElementById('app-nav') || document.querySelector('header');
    if (!header) { header = document.createElement('header'); document.body.prepend(header); }
    header.id = 'app-nav';
    header.removeAttribute('class');
    header.innerHTML = `
      <a class="nx-logo" href="${AVAILABLE.includes('/projects') ? '/projects' : '/agent-dashboard'}"><em>Negotiate</em>X.ai</a>
      <nav class="nx-links" aria-label="Hauptnavigation">
        ${links.map(l => `<a class="nx-link${l.primary ? ' primary' : ''}${isOn(l) ? ' on' : ''}" href="${l.href}">${esc(l.label)}${l.badge ? '<span class="nx-badge" id="nx-approvals" hidden></span>' : ''}</a>`).join('')}
      </nav>
      <div class="nx-right">
        <span class="nx-user">${esc(user && (user.email || user.name) || '')}</span>
        <a class="nx-btn" href="/dashboard" title="Bisheriges Analyse-Dashboard">Klassisch</a>
        <button class="nx-btn" id="nx-logout">Abmelden</button>
        <button class="nx-btn nx-menu" id="nx-menu" aria-label="Menue">Menue</button>
      </div>`;

    header.querySelector('#nx-menu').addEventListener('click', () => header.classList.toggle('open'));
    header.querySelectorAll('.nx-link').forEach(a => a.addEventListener('click', () => {
      header.classList.remove('open');
      // gleiche Seite, nur Hash: Router der Seite reagiert auf hashchange
    }));
    header.querySelector('#nx-logout').addEventListener('click', () => {
      try { localStorage.removeItem('nx_token'); localStorage.removeItem('nx_user'); } catch (e) {}
      location.href = '/login';
    });
    loadBadge();
    window.addEventListener('hashchange', () => {
      header.querySelectorAll('.nx-link').forEach((a, i) => a.classList.toggle('on', isOn(links[i])));
    });
  }

  async function loadBadge() {
    const token = localStorage.getItem('nx_token');
    if (!token) return;
    try {
      const r = await fetch('/api/v1/agent/overview', { headers: { Authorization: 'Bearer ' + token } });
      if (!r.ok) return;
      const n = (await r.json()).pending_count || 0;
      const b = document.getElementById('nx-approvals');
      if (b && n > 0) { b.textContent = n > 99 ? '99+' : n; b.hidden = false; }
    } catch (e) { /* Zaehler ist Komfort -- Navigation funktioniert ohne */ }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', render);
  else render();
})();
