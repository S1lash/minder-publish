/* Progressive enhancement: native form state with a keyboard-operable picker. */
(() => {
  'use strict';
  const mounted = new WeakMap();
  let active = null;
  const hints = {
    "private": "Only the signed-in owner can view this material.",
    "link": "Anyone with the link can open it until access expires.",
    "password": "Recipients need the publication password. Your owner login stays separate.",
    "immediate": "Close access at expiry and queue file deletion without a recovery period.",
    "after": "Keep files for the chosen recovery period after access expires.",
    "never": "Keep files until you delete them. Expired links still stop working.",
    "keep": "Keep the title, description and dates in your library after the files are deleted.",
    "with_content": "Remove the library entry when its files are actually deleted.",
    "metadata_after": "Keep the entry for an additional period, counted from actual file deletion.",
    "allow": "The site may load HTTPS resources and call external APIs from the visitor’s browser.",
    "self_only": "Load resources from this build. External fonts, CDNs and APIs may stop working.",
    "strict": "Serve existing files and directory indexes. Missing paths return 404.",
    "spa": "Use the entry for page navigation only. Missing scripts, styles and media stay 404."
  };
  function hint(select, value) {
    let key = value;
    if (/metadata/.test(select.id) && ['after', 'after_purge'].includes(value)) key = 'metadata_after';
    if (value === 'keep' && !/metadata/.test(select.id)) return '';
    if (value === 'after' && !/purge|metadata/.test(select.id)) return '';
    if (value === 'never' && !/purge/.test(select.id)) return '';
    if (!/access|purge|metadata|network|resources|routing/.test(select.id)) return '';
    return hints[key] ? (window.PublishTranslate?.(hints[key]) || hints[key]) : '';
  }
  function close(restore = false) {
    if (!active) return;
    const current = active; active = null;
    current.popup.remove(); current.button.setAttribute('aria-expanded', 'false');
    if (restore) current.button.focus();
  }
  function position(current) {
    const rect = current.button.getBoundingClientRect();
    const below = innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const upwards = below < 180 && above > below;
    const height = Math.min(360, Math.max(100, upwards ? above : below));
    const popupWidth = Math.min(Math.max(rect.width, current.select.id === 'language-select' ? 180 : 0), innerWidth - 24);
    current.popup.style.width = `${popupWidth}px`;
    current.popup.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - popupWidth - 12))}px`;
    current.popup.style.maxHeight = `${height}px`;
    current.popup.style.top = upwards ? 'auto' : `${rect.bottom + 6}px`;
    current.popup.style.bottom = upwards ? `${innerHeight - rect.top + 6}px` : 'auto';
  }
  function enhance(select) {
    if (mounted.has(select) || select.multiple || select.size > 1) return;
    const host = document.createElement('div'); host.className = 'select-control';
    select.before(host); host.append(select); select.classList.add('enhanced-select');
    select.tabIndex = -1; select.setAttribute('aria-hidden', 'true');
    const button = document.createElement('button'); button.type = 'button'; button.className = 'select-trigger';
    button.id = `${select.id || `select-${document.querySelectorAll('.select-trigger').length}`}-trigger`;
    button.setAttribute('role', 'combobox'); button.setAttribute('aria-haspopup', 'listbox'); button.setAttribute('aria-expanded', 'false');
    const text = document.createElement('span'); text.className = 'select-value'; button.append(text);
    const chevron = document.createElement('span'); chevron.className = 'select-chevron'; chevron.setAttribute('aria-hidden', 'true'); button.append(chevron);
    host.append(button);
    const label = select.id && document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
    if (label) { label.htmlFor = button.id; label.id ||= `${button.id}-label`; button.setAttribute('aria-labelledby', `${label.id} ${button.id}-value`); text.id = `${button.id}-value`; }
    const help = document.createElement('small'); help.className = 'select-help'; help.id = `${button.id}-help`; host.append(help);
    const current = { select, host, button, text, help, popup: null };
    mounted.set(select, current);
    const sync = () => {
      text.textContent = select.selectedOptions[0]?.textContent || '—';
      button.disabled = select.disabled;
      button.setAttribute('aria-invalid', select.getAttribute('aria-invalid') || 'false');
      if (!label) button.setAttribute('aria-label', `${select.getAttribute('aria-label') || select.name || ''}: ${text.textContent}`);
      const explanation = hint(select, select.value); help.textContent = explanation; help.hidden = !explanation;
      const described = [select.getAttribute('aria-describedby'), explanation ? help.id : null].filter(Boolean).join(' ');
      if (described) button.setAttribute('aria-describedby', described); else button.removeAttribute('aria-describedby');
    };
    let typed = '', typedAt = 0;
    const open = () => {
      if (button.disabled) return;
      if (active === current) { close(); return; }
      close(); active = current;
      const popup = document.createElement('div'); popup.className = 'select-popover'; popup.id = `${button.id}-list`; popup.setAttribute('role', 'listbox');
      popup.setAttribute('aria-label', label?.textContent || select.getAttribute('aria-label') || text.textContent);
      current.popup = popup; button.setAttribute('aria-controls', popup.id); button.setAttribute('aria-expanded', 'true');
      [...select.options].forEach(option => {
        const item = document.createElement('button'); item.type = 'button'; item.className = 'select-option'; item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', String(option.selected)); item.disabled = option.disabled;
        const name = document.createElement('span'); name.textContent = option.textContent; item.append(name);
        const explanation = hint(select, option.value);
        if (explanation) { const description = document.createElement('small'); description.textContent = explanation; item.append(description); }
        item.onclick = () => { select.value = option.value; select.dispatchEvent(new Event('input', { bubbles: true })); select.dispatchEvent(new Event('change', { bubbles: true })); sync(); close(true); };
        item.onkeydown = event => {
          const entries = [...popup.querySelectorAll('button:not(:disabled)')]; const index = entries.indexOf(item);
          if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) { event.preventDefault(); entries[event.key === 'Home' ? 0 : event.key === 'End' ? entries.length-1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % entries.length]?.focus(); }
          if (event.key === 'Escape') { event.preventDefault(); close(true); }
          if (event.key === 'Tab') {
            const dialog = button.closest('dialog');
            close(true);
            if (dialog) {
              event.preventDefault();
              const targets = [...dialog.querySelectorAll('a[href],button,input,textarea,select,summary,iframe,[contenteditable],[tabindex]')]
                .filter(element => element.tabIndex >= 0 && element.getClientRects().length && !element.closest('[hidden]'))
                .sort((a, b) => (a.tabIndex || Infinity) - (b.tabIndex || Infinity));
              const start = targets.indexOf(button), direction = event.shiftKey ? -1 : 1;
              if (start >= 0) for (let step = 1; step <= targets.length; step++) {
                const next = targets[(start + direction * step + targets.length) % targets.length];
                next.focus();
                if (document.activeElement === next) break;
              }
            }
          }
          if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && event.key !== ' ') {
            const time = Date.now(); typed = time - typedAt > 700 ? event.key : typed + event.key; typedAt = time;
            const match = entries.find(entry => entry.firstElementChild.textContent.toLocaleLowerCase().startsWith(typed.toLocaleLowerCase()));
            if (match) { event.preventDefault(); match.focus(); }
          }
        };
        popup.append(item);
      });
      (select.closest('dialog') || document.body).append(popup); position(current);
      const selected = popup.querySelector('[aria-selected=true]:not(:disabled)') || popup.querySelector('button:not(:disabled)'); selected?.focus();
    };
    select.addEventListener('focus', () => button.focus());
    button.onclick = open;
    button.onkeydown = event => { if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) { event.preventDefault(); open(); } if (event.key === 'Escape') close(true); };
    select.addEventListener('change', sync); select.addEventListener('input', sync);
    new MutationObserver(sync).observe(select, { attributes: true, childList: true, characterData: true, subtree: true });
    current.sync = sync; sync();
  }
  const mount = () => document.querySelectorAll('select').forEach(enhance);
  new MutationObserver(() => { mount(); if (active && !active.select.isConnected) close(); }).observe(document.body, {childList:true,subtree:true});
  document.addEventListener('pointerdown', event => { if (active && !active.popup.contains(event.target) && !active.button.contains(event.target)) close(); });
  const refresh = () => { close(true); document.querySelectorAll('select').forEach(select => mounted.get(select)?.sync()); };
  document.addEventListener('publish:languagechange', refresh);
  document.addEventListener('publish:formchange', refresh);
  document.addEventListener('scroll', event => {
    if (!active || active.popup.contains(event.target)) return;
    const rect = active.button.getBoundingClientRect();
    if (!active.button.isConnected || rect.bottom < 0 || rect.top > innerHeight) close();
    else position(active);
  }, true);
  document.addEventListener('close', () => close(), true);
  window.addEventListener('resize', () => close());
  mount();
})();
