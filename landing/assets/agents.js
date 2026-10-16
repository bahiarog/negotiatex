/* Digitales Team: Avatare der NegotiateX-Agenten (einheitlicher SVG-Stil).
 * NX_AGENTS.load()  -> Profile (einmal je Sitzung vom Server)
 * NX_AGENTS.avatar(profile, size) -> SVG-Markup
 * Die Figuren sind bewusst als digitale Mitarbeiter erkennbar (Visier, Antenne). */
(function () {
  const ICONS = {
    clipboard: '<rect x="2.5" y="1.5" width="9" height="11" rx="1.5" fill="none" stroke="C" stroke-width="1.6"/><path d="M5 5h4M5 7.5h4M5 10h2.5" stroke="C" stroke-width="1.4" stroke-linecap="round"/>',
    search: '<circle cx="6" cy="6" r="3.6" fill="none" stroke="C" stroke-width="1.8"/><path d="M8.7 8.7l3 3" stroke="C" stroke-width="2" stroke-linecap="round"/>',
    mail: '<rect x="1.5" y="3" width="11" height="8" rx="1.4" fill="none" stroke="C" stroke-width="1.6"/><path d="M2 4l5 3.6L12 4" fill="none" stroke="C" stroke-width="1.5" stroke-linejoin="round"/>',
    check: '<path d="M2.5 7.5l3 3 6-7" fill="none" stroke="C" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>',
    handshake: '<path d="M2 5h8.5M8 2.5l2.5 2.5L8 7.5M12 9H3.5M6 6.5L3.5 9 6 11.5" fill="none" stroke="C" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    scale: '<path d="M7 2v10M3.5 12h7M2 4.5h10" stroke="C" stroke-width="1.5" stroke-linecap="round"/><path d="M2 4.5L0.8 8h2.4zM12 4.5L10.8 8h2.4z" fill="C"/>',
    chart: '<path d="M2.5 12V8M6 12V4.5M9.5 12V6.5M12 12H1.5" stroke="C" stroke-width="1.9" stroke-linecap="round"/>',
  };

  function esc(s) { return (s ?? '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  function avatar(a, size, status) {
    const c = (a && a.color) || '#5d6a63';
    const icon = (ICONS[a && a.icon] || ICONS.check).replace(/="C"/g, `="${c}"`);
    const dot = status ? `<circle cx="52" cy="12" r="6.5" fill="${status === 'working' ? '#2fae7a' : status === 'waiting' ? '#e0a42e' : '#a7b0aa'}" stroke="#fff" stroke-width="2.5">${status === 'working' ? '<animate attributeName="opacity" values="1;.45;1" dur="1.6s" repeatCount="indefinite"/>' : ''}</circle>` : '';
    return `<svg class="nx-avatar-svg" width="${size || 56}" height="${size || 56}" viewBox="0 0 64 64" role="img" aria-label="${esc(a ? a.name : 'Agent')} (digitaler Mitarbeiter)">
      <rect width="64" height="64" rx="18" fill="${c}"/>
      <path d="M10 64c3-11 12-16 22-16s19 5 22 16z" fill="#fff" opacity=".22"/>
      <line x1="32" y1="14" x2="32" y2="8" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="32" cy="7" r="2.6" fill="#fff"/>
      <circle cx="32" cy="31" r="16.5" fill="#fff"/>
      <rect x="19.5" y="24.5" width="25" height="10" rx="5" fill="${c}"/>
      <circle cx="26.5" cy="29.5" r="2.1" fill="#fff"/><circle cx="37.5" cy="29.5" r="2.1" fill="#fff"/>
      <path d="M26.5 39.5q5.5 3.6 11 0" stroke="${c}" stroke-width="2.2" fill="none" stroke-linecap="round"/>
      <circle cx="50" cy="50" r="11" fill="#fff"/><g transform="translate(43 43)">${icon}</g>
      ${dot}</svg>`;
  }

  let cache = null;
  async function load() {
    if (cache) return cache;
    try { const s = sessionStorage.getItem('nx_agent_profiles'); if (s) { cache = JSON.parse(s); return cache; } } catch (e) {}
    const token = localStorage.getItem('nx_token');
    const r = await fetch('/api/v1/agents/profiles', { headers: { Authorization: 'Bearer ' + token } });
    if (!r.ok) return [];
    cache = await r.json();
    try { sessionStorage.setItem('nx_agent_profiles', JSON.stringify(cache)); } catch (e) {}
    return cache;
  }
  function byKey(list, key) { return (list || []).find(a => a.key === key) || null; }

  window.NX_AGENTS = { avatar, load, byKey, esc };
})();
