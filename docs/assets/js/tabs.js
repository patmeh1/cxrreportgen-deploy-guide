/* Tabs: keyboard-accessible, deep-linkable, sync with mobile <select>. */
(function () {
  const buttons = Array.from(document.querySelectorAll('.tab-button'));
  const panels  = Array.from(document.querySelectorAll('.tab-panel'));
  const select  = document.getElementById('tab-select');

  function show(id, push) {
    buttons.forEach(b => {
      const active = b.dataset.tab === id;
      b.setAttribute('aria-selected', active ? 'true' : 'false');
      b.tabIndex = active ? 0 : -1;
    });
    panels.forEach(p => p.classList.toggle('active', p.id === 'tab-' + id));
    if (select && select.value !== id) select.value = id;
    if (push) history.replaceState(null, '', '#tab=' + id);
    document.querySelector('#tab-' + id)?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.tab, true)));

  buttons.forEach((b, i) => {
    b.addEventListener('keydown', e => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const dir = e.key === 'ArrowRight' ? 1 : -1;
        const next = buttons[(i + dir + buttons.length) % buttons.length];
        show(next.dataset.tab, true);
        next.focus();
      } else if (e.key === 'Home') { e.preventDefault(); show(buttons[0].dataset.tab, true); buttons[0].focus(); }
      else if (e.key === 'End')   { e.preventDefault(); show(buttons[buttons.length-1].dataset.tab, true); buttons[buttons.length-1].focus(); }
    });
  });

  if (select) select.addEventListener('change', e => show(e.target.value, true));

  // Inner tabs (deploy step)
  document.querySelectorAll('.inner-tabs').forEach(group => {
    const btns = Array.from(group.querySelectorAll('.inner-tab-btn'));
    const parent = group.parentElement;
    const pans = Array.from(parent.querySelectorAll(':scope > .inner-tab-panel'));
    btns.forEach(btn => btn.addEventListener('click', () => {
      btns.forEach(b => b.setAttribute('aria-selected', b === btn ? 'true' : 'false'));
      pans.forEach(p => p.classList.toggle('active', p.dataset.inner === btn.dataset.inner));
    }));
  });

  // Deep link
  const fromHash = (location.hash.match(/tab=([\w-]+)/) || [])[1];
  show(fromHash && document.getElementById('tab-' + fromHash) ? fromHash : 'overview', false);

  // Theme toggle
  const themeBtn = document.getElementById('theme-toggle');
  const stored = localStorage.getItem('theme');
  if (stored) document.documentElement.setAttribute('data-theme', stored);
  themeBtn?.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme')
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
  });

  // Copy buttons
  document.querySelectorAll('pre > code').forEach(code => {
    const pre = code.parentElement;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.type = 'button';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        btn.textContent = 'Copied'; btn.classList.add('copied');
        setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1500);
      } catch { btn.textContent = 'Failed'; }
    });
    pre.appendChild(btn);
  });
})();
