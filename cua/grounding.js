// Injected into every frame. Turns a screen point (what the model sees) into
// locator candidates (what replay needs), and reports human actions + control-bar clicks.
(() => {
  if (window.__cua) return;
  const ACTIONABLE = 'a[href],button,input,select,textarea,label,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[onclick],[contenteditable=true]';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const inBar = el => !!(el && el.closest && el.closest('#__cua_bar'));

  function deepHit(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  // mode 'act': promote to the clickable/editable ancestor. mode 'read': smallest element with text.
  function hit(x, y, mode) {
    let el = deepHit(x, y);
    if (!el || inBar(el)) return null;
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return el;  // caller descends
    if (mode === 'act') return el.closest(ACTIONABLE) || el;
    while (el.parentElement && !clean(el.innerText)) el = el.parentElement;
    return el;
  }

  function focused() {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return el && el !== document.body ? el : null;
  }

  function role(el) {
    const r = el.getAttribute('role');
    if (r) return r.split(' ')[0];
    const t = el.tagName.toLowerCase(), type = (el.getAttribute('type') || 'text').toLowerCase();
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button' || (t === 'input' && ['submit', 'button', 'reset', 'image'].includes(type))) return 'button';
    if (t === 'input' && (type === 'checkbox' || type === 'radio')) return type;
    if (t === 'input' && ['text', 'email', 'tel', 'url'].includes(type)) return 'textbox';
    if (t === 'input' && type === 'search') return 'searchbox';
    if (t === 'input' && type === 'number') return 'spinbutton';
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    if (/^h[1-6]$/.test(t)) return 'heading';
    return null;
  }

  function labelText(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const ids = el.getAttribute('aria-labelledby');
    const n = ids && document.getElementById(ids.split(' ')[0]);
    return n ? clean(n.innerText) : '';
  }

  // Approximate accessible name. Python verifies every candidate, so a wrong guess is dropped, never used.
  function name(el) {
    const t = el.tagName;
    const isBtnInput = t === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type);
    return clean(el.getAttribute('aria-label')) || labelText(el) || (isBtnInput ? clean(el.value) : '') ||
      (['A', 'BUTTON', 'SUMMARY'].includes(t) || /^H[1-6]$/.test(t) ? clean(el.innerText) : '') ||
      clean(el.getAttribute('title')) || clean(el.getAttribute('placeholder'));
  }

  // The nearest visible text before the element in document order: the "label" of legacy table forms.
  function anchor(el) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let last = '', n;
    while ((n = w.nextNode())) {
      if (el.contains(n) || (el.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      const p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|OPTION)$/.test(p.tagName) || inBar(p) || !visible(p)) continue;
      const t = clean(n.textContent);
      if (t && t.length <= 60) last = t;
    }
    return last;
  }

  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      if (el.id && /^[A-Za-z][\w-]*$/.test(el.id) && !/\d{3,}/.test(el.id)) { parts.unshift('#' + el.id); break; }
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    return parts.join(' > ');
  }

  // Ordered most-semantic -> most-structural. This order IS the replay ladder.
  function describe(el, mode) {
    const r = role(el), n = name(el), text = clean(el.innerText).slice(0, 80), tag = el.tagName.toLowerCase();
    const c = [];
    if (r && n) c.push({by: 'role', role: r, value: n});
    const lt = labelText(el);
    if (lt) c.push({by: 'label', value: lt});
    const ph = clean(el.getAttribute('placeholder'));
    if (ph) c.push({by: 'placeholder', value: ph});
    if (mode === 'act' && text && text.length <= 60 && !['input', 'textarea', 'select'].includes(tag)) c.push({by: 'text', value: text});
    const a = anchor(el);
    if (a) c.push({by: 'near_text', value: a, tag});
    const nm = el.getAttribute('name');
    if (nm && ['input', 'select', 'textarea', 'button'].includes(tag)) c.push({by: 'css', value: `${tag}[name="${nm}"]`});
    c.push({by: 'css', value: cssPath(el)});
    // Enclosing repeated containers (one of several same-kind siblings: a table row, a list item, a product card),
    // innermost first, with their text minus the element's own: candidates for "the <inner> in the row for X".
    const outside = p => {
      const w = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
      let t = '', n;
      while ((n = w.nextNode())) if (!el.contains(n) && !/^(SCRIPT|STYLE)$/.test(n.parentElement.tagName)) t += ' ' + n.textContent;
      return clean(t).slice(0, 500);
    };
    const rows = [];
    for (let p = el.parentElement; p && p !== document.body && rows.length < 8; p = p.parentElement) {
      const cls = [...p.classList].find(k => !/\d/.test(k));
      const sel = p.tagName.toLowerCase() + (cls ? '.' + CSS.escape(cls) : '');
      const kin = p.parentElement ? [...p.parentElement.children].filter(x => x.matches(sel) && !inBar(x)).length : 0;
      if (kin >= 2) rows.push({sel, text: outside(p)});
    }
    return {candidates: c, rows, tag, role: r, name: n, anchor: a, text, password: el.type === 'password',
            frame_selector: el.name ? `${tag}[name="${el.name}"]` : cssPath(el)};
  }

  // Page state for checkpoints: the most prominent visible heading (h1 before h2 before the rest).
  function heading() {
    for (const sel of ['h1', 'h2', 'h3,.title,legend,caption']) {
      for (const h of document.querySelectorAll(sel)) {
        if (visible(h) && !inBar(h) && clean(h.innerText)) return clean(h.innerText).slice(0, 80);
      }
    }
    return '';
  }

  // Every short visible text on the page: checkpoint candidates, verified against before/after a step.
  function texts() {
    const out = new Set(), w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode()) && out.size < 300) {
      const p = n.parentElement, t = clean(n.textContent);
      if (t && t.length <= 80 && p && !/^(SCRIPT|STYLE|NOSCRIPT|OPTION)$/.test(p.tagName) && !inBar(p) && visible(p)) out.add(t);
    }
    return [...out];
  }

  window.__cua = {hit, focused, describe, heading, texts};

  // Human actions (only recorded by Python while a human holds control). Described synchronously,
  // because a click may navigate away before Python sees it. Password values never leave the page.
  const report = (kind, el) => {
    if (!window.__cua_event || !el || inBar(el)) return;
    const info = describe(el, 'act');
    if (el.tagName === 'SELECT') kind = 'select';
    const value = kind === 'select' ? clean(el.selectedOptions[0] && el.selectedOptions[0].text) :
      kind === 'type' ? (info.password ? '[redacted]' : el.value) : null;
    window.__cua_event({kind, info, value});
  };
  document.addEventListener('click', e => {
    const t = e.composedPath()[0];
    if (t && t.closest && !['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) report('click', t.closest(ACTIONABLE) || t);
  }, true);
  document.addEventListener('change', e => report('type', e.composedPath()[0]), true);

  // Control bar: top frame only. Python calls __cua.render(state) on every control change.
  if (window !== window.top) return;
  const esc = s => clean(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  // Closed shadow root: page CSS can't restyle it, and page text queries (checkpoints) can't see its text.
  window.__cua.render = s => {
    window.__cua_state = s;
    if (!document.body || document.body.tagName === 'FRAMESET') return;
    let host = document.getElementById('__cua_bar');
    if (!host) {
      host = document.createElement('div');
      host.id = '__cua_bar';
      host.__root = host.attachShadow({mode: 'closed'});
      document.body.appendChild(host);
    }
    const human = s.owner === 'human', waiting = s.state === 'awaiting_human';
    const bg = human ? '#fff4d6' : waiting ? '#ffe1e1' : '#e6f0ff';
    const btn = (id, label) => `<button data-a="${id}">${label}</button>`;
    const style = `<style>:host{all:initial}div{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:${bg};color:#111;
      font:14px system-ui,sans-serif;padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;box-shadow:0 -2px 8px #0003;pointer-events:none}
      button{padding:6px 12px;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer;font:inherit}
      input,select{padding:5px;font:inherit}
      button,input,select{pointer-events:auto}</style>`;  // the bar never blocks clicks on the page
    const root = host.__root;
    let html = `<b>${human ? '🧑 You\'re in control' : waiting ? '⚠️ Needs you' : '🤖 Agent in control'}</b><span style="flex:1">${esc(s.message)}</span>`;
    if (waiting && s.ask === 'approval') html += btn('approve', 'Approve') + btn('deny', 'Deny');
    if (!human) html += btn('take_over', 'Take over');
    if (human) html += btn('hand_back', 'Hand back') + (s.can_label ? btn('label_open', 'This screen means…') : '');
    root.innerHTML = `${style}<div>${html}</div>`;
    root.querySelectorAll('button').forEach(b => b.onclick = () => {
      const a = b.dataset.a;
      if (a !== 'label_open') return window.__cua_ui(a, {});
      const picked = clean(String(window.getSelection())) || heading();
      root.innerHTML = `${style}<div><b>This screen means…</b>
        <select id="k">
          <option value="business_outcome">A normal answer (e.g. not found)</option>
          <option value="dismiss">A pop-up I closed: close it and continue</option>
          <option value="retry">Temporary: retry this step</option>
          <option value="restart">Logged out: start over</option>
          <option value="failure">An error: stop</option>
        </select>
        <input id="c" placeholder="short code, e.g. NOT_FOUND" style="width:170px">
        <input id="t" value="${esc(picked)}" title="Text that identifies this screen (select text on the page first)" style="flex:1">
        ${btn('label', 'Save')}${btn('cancel', 'Cancel')}</div>`;
      root.querySelector('[data-a=cancel]').onclick = () => window.__cua.render(window.__cua_state);
      root.querySelector('[data-a=label]').onclick = () => window.__cua_ui('label', {
        kind: root.querySelector('#k').value,
        code: clean(root.querySelector('#c').value).toUpperCase().replace(/\W+/g, '_'),
        text: clean(root.querySelector('#t').value)});
    });
  };
  const rerender = () => window.__cua_state && window.__cua.render(window.__cua_state);
  document.addEventListener('DOMContentLoaded', rerender);
})();
