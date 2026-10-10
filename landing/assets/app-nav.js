/* Gemeinsame Kopfleiste aller App-Seiten (Kunden- und Arbeitsbereich).
 * Menue und Bereichswechsel werden aus den Rollen abgeleitet (GET /api/v1/me/access).
 * Das ist reine Bedienfuehrung -- die Rechte prueft der Server fuer jede Route. */
(function () {
  const CUSTOMER = [
    { key: 'uebersicht', href: '/uebersicht', label: 'Übersicht' },
    { key: 'vorhaben', href: '/vorhaben', label: 'Meine Vorhaben' },
    { key: 'einsparungen', href: '/einsparungen', label: 'Einsparungen' },
    { key: 'entscheidungen', href: '/entscheidungen', label: 'Entscheidungen', badge: 'decisions' },
  ];
  const WORKSPACE = [
    { key: 'arbeitsuebersicht', href: '/arbeitsbereich', label: 'Arbeitsübersicht', role: 'procurement', badge: 'pending' },
    { key: 'ws-vorhaben', href: '/arbeitsbereich/vorhaben', label: 'Vorhaben & Aufgaben', role: 'procurement' },
    { key: 'lieferanten', href: '/sourcing-dashboard', label: 'Lieferanten & Ausschreibungen', role: 'procurement' },
    { key: 'bestellungen', href: '/rfq-dashboard', label: 'Bestellungen & Verträge', role: 'procurement' },
    { key: 'datacenter', href: '/data-center', label: 'Data Center', role: 'procurement' },
    { key: 'administration', href: '/administration', label: 'Administration', role: 'admin' },
  ];

  /* Seite -> Bereich + markierter Menuepunkt. Prozessschritt-Seiten (z.B. Verhandlungen)
   * haben keinen eigenen Menuepunkt, sondern markieren ihren uebergeordneten Bereich. */
  function locate(path) {
    const p = path.replace(/\/+$/, '') || '/';
    const ws = (key, role) => ({ area: 'workspace', key, role: role || 'procurement' });
    if (p === '/arbeitsbereich' || p === '/agent-dashboard') return ws('arbeitsuebersicht');
    if (p.startsWith('/arbeitsbereich/vorhaben') || p === '/cases-dashboard') return ws('ws-vorhaben');
    if (p === '/sourcing-dashboard') return ws('lieferanten');
    if (p === '/rfq-dashboard') return ws('bestellungen');
    if (p === '/data-center') return ws('datacenter');
    if (p === '/administration') return ws('administration', 'admin');
    if (p.startsWith('/vorhaben')) return { area: 'customer', key: 'vorhaben' };
    if (p.startsWith('/einsparungen')) return { area: 'customer', key: 'einsparungen' };
    if (p.startsWith('/entscheidungen')) return { area: 'customer', key: 'entscheidungen' };
    if (p.startsWith('/hilfe')) return { area: 'customer', key: 'hilfe' };
    return { area: 'customer', key: 'uebersicht' };
  }

  const css = `
  #app-nav{position:fixed;top:0;left:0;right:0;z-index:300;height:60px;background:rgba(247,246,241,.96);backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:16px;font-family:'DM Sans',sans-serif}
  #app-nav nav{position:static;padding:0;background:none;backdrop-filter:none;border:none;z-index:auto;justify-content:flex-start}
  #app-nav a:focus-visible,#app-nav button:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
  #app-nav .nx-logo{font-family:'Playfair Display',serif;font-size:18px;font-weight:700;color:var(--text);text-decoration:none;flex-shrink:0}
  #app-nav .nx-logo em{color:var(--gold);font-style:normal}
  #app-nav .nx-area{font-size:10.5px;letter-spacing:.8px;text-transform:uppercase;color:var(--text-dim);border-left:1px solid var(--border);padding-left:12px;flex-shrink:0}
  #app-nav .nx-links{display:flex;align-items:center;gap:2px;flex:1;min-width:0}
  #app-nav .nx-link{font-size:13.5px;color:var(--text-muted);text-decoration:none;padding:8px 11px;border-radius:7px;white-space:nowrap;display:inline-flex;align-items:center;gap:6px;position:relative}
  #app-nav .nx-link:hover{color:var(--text);background:rgba(0,0,0,.035)}
  #app-nav .nx-link[aria-current=page]{color:var(--text);font-weight:500}
  #app-nav .nx-link[aria-current=page]:after{content:'';position:absolute;left:11px;right:11px;bottom:-11px;height:2px;background:var(--gold);border-radius:2px}
  #app-nav .nx-badge{font-family:'DM Mono',monospace;font-size:10.5px;background:var(--gold);color:#fff;border-radius:9px;padding:1px 7px;line-height:16px}
  #app-nav .nx-right{display:flex;align-items:center;gap:8px;margin-left:auto;flex-shrink:0}
  #app-nav .nx-new{font-size:13px;color:#fff;background:var(--gold);border:none;padding:8px 14px;border-radius:7px;text-decoration:none;white-space:nowrap;font-weight:500}
  #app-nav .nx-new:hover{background:var(--gold-light)}
  #app-nav .nx-btn{font-size:12.5px;color:var(--text-muted);background:transparent;border:1px solid var(--border);padding:6px 11px;border-radius:7px;cursor:pointer;text-decoration:none;font-family:inherit;white-space:nowrap}
  #app-nav .nx-btn:hover{color:var(--text);border-color:var(--gold-border)}
  #app-nav .nx-switch{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
  #app-nav .nx-switch a{font-size:12px;padding:6px 10px;color:var(--text-muted);text-decoration:none;white-space:nowrap}
  #app-nav .nx-switch a[aria-current=true]{background:var(--text);color:#fff}
  #app-nav .nx-avatar{width:34px;height:34px;border-radius:50%;border:1px solid var(--border);background:var(--bg-card);cursor:pointer;font-family:'DM Mono',monospace;font-size:12px;color:var(--text)}
  #app-nav .nx-menu-btn{display:none}
  #app-nav .nx-pop{position:absolute;top:56px;right:16px;width:280px;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;box-shadow:0 18px 40px rgba(0,0,0,.12);padding:8px;display:none}
  #app-nav .nx-pop.open{display:block}
  #app-nav .nx-pop .who{padding:10px 10px 12px;border-bottom:1px solid var(--border);margin-bottom:6px}
  #app-nav .nx-pop .who b{display:block;font-size:14px}
  #app-nav .nx-pop .who span{display:block;font-size:12px;color:var(--text-muted)}
  #app-nav .nx-pop .tag{display:inline-block;font-size:10.5px;font-family:'DM Mono',monospace;border:1px solid var(--border);border-radius:5px;padding:1px 6px;margin:6px 4px 0 0;color:var(--text-muted)}
  #app-nav .nx-pop a,#app-nav .nx-pop button{display:block;width:100%;text-align:left;padding:9px 10px;border-radius:6px;font-size:13px;color:var(--text);text-decoration:none;background:none;border:none;cursor:pointer;font-family:inherit}
  #app-nav .nx-pop a:hover,#app-nav .nx-pop button:hover{background:var(--bg-card2)}
  .nx-notice{position:fixed;top:68px;left:50%;transform:translateX(-50%);z-index:400;background:var(--text);color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif}
  @media(max-width:1180px){
    #app-nav .nx-links{display:none;position:absolute;top:60px;left:0;right:0;flex-direction:column;align-items:stretch;background:var(--bg);
      border-bottom:1px solid var(--border);padding:10px 16px;gap:2px;box-shadow:0 12px 24px rgba(0,0,0,.06)}
    #app-nav.open .nx-links{display:flex}
    #app-nav .nx-link{padding:12px}
    #app-nav .nx-link[aria-current=page]:after{display:none}
    #app-nav .nx-link[aria-current=page]{background:var(--bg-card)}
    #app-nav .nx-menu-btn{display:inline-block}
    #app-nav .nx-area,#app-nav .nx-switch{display:none}
    #app-nav .nx-links .nx-mobile-switch{display:flex;gap:8px;padding:8px 4px;border-top:1px solid var(--border);margin-top:6px}
  }
  @media(min-width:1181px){#app-nav .nx-mobile-switch{display:none}}
  @media(max-width:1560px){#app-nav .nx-switch,#app-nav .nx-area{display:none}}
  @media(min-width:1561px){#app-nav .nx-pop .nx-pop-switch{display:none}}
  @media(min-width:1181px) and (max-width:1400px){#app-nav .nx-link{padding:8px 8px;font-size:13px}#app-nav{gap:10px}}
  @media(max-width:560px){#app-nav .nx-new{padding:8px 10px}#app-nav .nx-new span{display:none}#app-nav .nx-help{display:none}}`;

  function esc(s) { return (s ?? '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  const token = (() => { try { return localStorage.getItem('nx_token'); } catch (e) { return null; } })();
  if (!token) { location.href = '/login'; return; }

  async function loadAccess() {
    let cached = null;
    try { cached = JSON.parse(sessionStorage.getItem('nx_access') || 'null'); } catch (e) {}
    const fresh = fetch('/api/v1/me/access', { headers: { Authorization: 'Bearer ' + token } }).then(async r => {
      if (r.status === 401) { try { localStorage.removeItem('nx_token'); } catch (e) {} location.href = '/login'; return null; }
      if (r.status === 403) { return null; }  // noch kein Mandant -- Seite legt ihn an
      const a = await r.json();
      try { sessionStorage.setItem('nx_access', JSON.stringify(a)); } catch (e) {}
      return a;
    }).catch(() => null);
    return { cached, fresh };
  }

  function initials(a) {
    const n = (a && (a.user.name || a.user.email)) || '?';
    return n.split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(x => x[0].toUpperCase()).join('');
  }

  function render(header, a, here) {
    const areas = (a && a.areas) || { customer: true };
    const inWs = here.area === 'workspace';
    const items = inWs ? WORKSPACE.filter(l => (l.role === 'admin' ? areas.admin : areas.procurement)) : CUSTOMER;
    const homeHref = inWs ? (areas.procurement ? '/arbeitsbereich' : '/administration') : '/uebersicht';
    const canSwitch = areas.workspace;
    const wsHref = areas.procurement ? '/arbeitsbereich' : '/administration';
    header.id = 'app-nav';
    header.removeAttribute('class');
    header.innerHTML = `
      <a class="nx-logo" href="${homeHref}" aria-label="Zur Übersicht"><em>Negotiate</em>X.ai</a>
      ${canSwitch ? `<span class="nx-area">${inWs ? 'Arbeitsbereich' : 'Kundenbereich'}</span>` : ''}
      <nav class="nx-links" id="nx-links" aria-label="Hauptnavigation">
        ${items.map(l => `<a class="nx-link" href="${l.href}" ${l.key === here.key ? 'aria-current="page"' : ''}>${esc(l.label)}${l.badge ? `<span class="nx-badge" data-badge="${l.badge}" hidden></span>` : ''}</a>`).join('')}
        ${canSwitch ? `<div class="nx-mobile-switch"><a class="nx-btn" href="/uebersicht">Kundenbereich</a><a class="nx-btn" href="${wsHref}">Arbeitsbereich</a></div>` : ''}
      </nav>
      <div class="nx-right">
        ${canSwitch ? `<div class="nx-switch" role="group" aria-label="Bereich wechseln"><a href="/uebersicht" aria-current="${!inWs}">Kundenbereich</a><a href="${wsHref}" aria-current="${inWs}">Arbeitsbereich</a></div>` : ''}
        <a class="nx-new" href="/vorhaben/neu">+ <span>Neues Vorhaben</span></a>
        <a class="nx-btn nx-help" href="/hilfe">Hilfe</a>
        <button class="nx-avatar" id="nx-profile" aria-haspopup="true" aria-expanded="false" aria-label="Profil">${esc(initials(a))}</button>
        <button class="nx-btn nx-menu-btn" id="nx-menu" aria-controls="nx-links" aria-expanded="false">Menü</button>
      </div>
      <div class="nx-pop" id="nx-pop" role="menu">
        <div class="who"><b>${esc(a ? (a.user.name || a.user.email) : '')}</b><span>${esc(a ? a.user.email : '')}</span>
          <span>${esc(a && a.tenant ? a.tenant.company_name || '' : '')}</span>
          ${a ? a.role_labels.map(r => `<span class="tag">${esc(r)}</span>`).join('') : ''}
          ${a ? `<span class="tag">${a.can_decide ? 'Entscheidungsbefugt' : 'Ohne Entscheidungsbefugnis'}</span>` : ''}</div>
        ${canSwitch ? `<a class="nx-pop-switch" href="${inWs ? '/uebersicht' : wsHref}" role="menuitem">${inWs ? 'Zum Kundenbereich wechseln' : 'Zum Arbeitsbereich wechseln'}</a>` : ''}
        <a href="/hilfe" role="menuitem">Hilfe</a>
        ${areas.admin ? '<a href="/administration" role="menuitem">Administration</a>' : ''}
        ${a && a.platform_admin ? '<a href="/dashboard" role="menuitem">Klassisches Dashboard (Plattform)</a>' : ''}
        <button id="nx-logout" role="menuitem">Abmelden</button>
      </div>`;

    const pop = header.querySelector('#nx-pop'), prof = header.querySelector('#nx-profile');
    prof.addEventListener('click', e => { e.stopPropagation(); const o = pop.classList.toggle('open'); prof.setAttribute('aria-expanded', o); });
    if (!window.__nxDocListeners) {
      window.__nxDocListeners = true;
      const close = () => { const h = document.getElementById('app-nav'); if (!h) return; const p = h.querySelector('#nx-pop'); if (p) p.classList.remove('open');
        const b = h.querySelector('#nx-profile'); if (b) b.setAttribute('aria-expanded', false); };
      document.addEventListener('click', e => { const p = document.getElementById('nx-pop'); if (p && !p.contains(e.target)) close(); });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') { close(); const h = document.getElementById('app-nav'); if (h) h.classList.remove('open'); } });
    }
    const mb = header.querySelector('#nx-menu');
    mb.addEventListener('click', () => { const o = header.classList.toggle('open'); mb.setAttribute('aria-expanded', o); });
    header.querySelector('#nx-logout').addEventListener('click', () => {
      try { localStorage.removeItem('nx_token'); localStorage.removeItem('nx_user'); sessionStorage.clear(); } catch (e) {}
      location.href = '/login';
    });
  }

  async function badges(header, a, here) {
    const set = (k, n) => { const b = header.querySelector(`[data-badge="${k}"]`); if (b) { b.textContent = n > 99 ? '99+' : n; b.hidden = !(n > 0); } };
    try {
      if (here.area === 'workspace' && a && a.areas.procurement) {
        const r = await fetch('/api/v1/agent/overview', { headers: { Authorization: 'Bearer ' + token } });
        if (r.ok) set('pending', (await r.json()).pending_count || 0);
      } else if (here.area !== 'workspace') {
        const r = await fetch('/api/v1/projects/decisions', { headers: { Authorization: 'Bearer ' + token } });
        if (r.ok) set('decisions', (await r.json()).length);
      }
    } catch (e) { /* Zaehler ist Komfort */ }
  }

  function guard(a, here) {
    if (!a || here.area !== 'workspace') return true;
    const ok = here.role === 'admin' ? a.areas.admin : a.areas.procurement;
    if (!ok) {
      try { sessionStorage.setItem('nx_notice', 'Dieser Bereich ist für Ihre Rolle nicht freigegeben.'); } catch (e) {}
      location.replace('/uebersicht');
      return false;
    }
    return true;
  }

  async function start() {
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    let header = document.getElementById('app-nav') || document.querySelector('header');
    if (!header) { header = document.createElement('header'); document.body.prepend(header); }
    const here = locate(location.pathname);
    const { cached, fresh } = await loadAccess();
    if (cached) { if (!guard(cached, here)) return; render(header, cached, here); }
    const a = await fresh;
    if (a) {
      window.NX_ACCESS = a;
      if (!guard(a, here)) return;
      render(header, a, here);
      badges(header, a, here);
      document.dispatchEvent(new CustomEvent('nx:access', { detail: a }));
    } else if (!cached) { render(header, null, here); }
    try {
      const n = sessionStorage.getItem('nx_notice');
      if (n) { sessionStorage.removeItem('nx_notice'); const d = document.createElement('div'); d.className = 'nx-notice'; d.setAttribute('role', 'status'); d.textContent = n; document.body.appendChild(d); setTimeout(() => d.remove(), 5000); }
    } catch (e) {}
  }
  window.NX_NAV = {
    locate,
    refreshBadges: () => { const h = document.getElementById('app-nav'); if (h && window.NX_ACCESS) badges(h, window.NX_ACCESS, locate(location.pathname)); },
    /* Fuer Einzelseiten-Navigation (pushState): Markierung und Bereich aktualisieren */
    update: () => {
      const h = document.getElementById('app-nav'); const here = locate(location.pathname);
      if (!h || !guard(window.NX_ACCESS, here)) return;
      render(h, window.NX_ACCESS || null, here); badges(h, window.NX_ACCESS, here);
    },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
