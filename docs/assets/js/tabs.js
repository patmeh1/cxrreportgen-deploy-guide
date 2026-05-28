/* Tabs: grouped dropdown menubar + mobile <select>, deep-linkable, keyboard-friendly. */
(function () {
  const buttons = Array.from(document.querySelectorAll('.tab-button'));
  const panels  = Array.from(document.querySelectorAll('.tab-panel'));
  const select  = document.getElementById('tab-select');
  const groups  = Array.from(document.querySelectorAll('.nav-group'));
  const toggles = Array.from(document.querySelectorAll('.nav-group-toggle'));
  const current = document.querySelector('.nav-current');

  function groupOf(btn) { return btn.closest('.nav-group'); }
  function toggleOf(g)  { return g?.querySelector('.nav-group-toggle'); }
  function itemsIn(g)   { return Array.from(g.querySelectorAll('.tab-button')); }

  function closeAllMenus(except) {
    groups.forEach(g => {
      if (g === except) return;
      g.classList.remove('open');
      const t = toggleOf(g); if (t) t.setAttribute('aria-expanded', 'false');
    });
  }
  function openMenu(g) {
    closeAllMenus(g);
    g.classList.add('open');
    toggleOf(g).setAttribute('aria-expanded', 'true');
  }

  function show(id, push) {
    buttons.forEach(b => {
      const active = b.dataset.tab === id;
      b.setAttribute('aria-selected', active ? 'true' : 'false');
      b.tabIndex = active ? 0 : -1;
    });
    panels.forEach(p => p.classList.toggle('active', p.id === 'tab-' + id));
    if (select && select.value !== id) select.value = id;
    if (push) history.replaceState(null, '', '#tab=' + id);

    const activeBtn = buttons.find(b => b.dataset.tab === id);
    const activeGroup = activeBtn ? groupOf(activeBtn) : null;
    groups.forEach(g => {
      const t = toggleOf(g); if (!t) return;
      t.classList.toggle('has-active', g === activeGroup);
    });
    if (current) {
      if (activeGroup && activeBtn) {
        const groupLabel = toggleOf(activeGroup).textContent.trim();
        const itemLabel  = activeBtn.textContent.trim();
        current.innerHTML = groupLabel + ' &rsaquo; <strong>' + itemLabel + '</strong>';
      } else { current.textContent = ''; }
    }
    closeAllMenus();
    document.querySelector('#tab-' + id)?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.tab, true)));

  // Group toggles: click opens/closes the menu
  toggles.forEach((t, i) => {
    t.addEventListener('click', e => {
      e.stopPropagation();
      const g = groupOf(t);
      if (g.classList.contains('open')) { closeAllMenus(); }
      else { openMenu(g); }
    });
    t.addEventListener('keydown', e => {
      const g = groupOf(t);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const dir = e.key === 'ArrowRight' ? 1 : -1;
        const next = toggles[(i + dir + toggles.length) % toggles.length];
        next.focus();
      } else if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openMenu(g);
        itemsIn(g)[0]?.focus();
      } else if (e.key === 'Escape') {
        closeAllMenus();
      }
    });
  });

  // Menu items: ArrowUp/Down within menu, Esc closes, click triggers show()
  groups.forEach(g => {
    const items = itemsIn(g);
    items.forEach((b, i) => {
      b.addEventListener('keydown', e => {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          items[(i + 1) % items.length].focus();
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          items[(i - 1 + items.length) % items.length].focus();
        } else if (e.key === 'Home') {
          e.preventDefault(); items[0].focus();
        } else if (e.key === 'End') {
          e.preventDefault(); items[items.length - 1].focus();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          closeAllMenus();
          toggleOf(g)?.focus();
        } else if (e.key === 'Tab') {
          // Let Tab close the menu naturally
          closeAllMenus();
        }
      });
    });
  });

  // Click outside closes
  document.addEventListener('click', e => {
    if (!e.target.closest('.nav-group')) closeAllMenus();
  });
  // Esc anywhere closes
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAllMenus();
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
