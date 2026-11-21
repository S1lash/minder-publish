/* Native preference forms work without this optional menu-dismissal enhancement. */
export function mountGatePreferences(root) {
  const menus = [...root.querySelectorAll('.preferences .preference')];
  const removeListeners = [];
  const listen = (target, name, handler) => {
    target.addEventListener(name, handler);
    removeListeners.push(() => target.removeEventListener(name, handler));
  };
  const closeOthers = current => {
    for (const menu of menus) if (menu !== current) menu.open = false;
  };
  const closeOutside = event => {
    for (const menu of menus) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    }
  };
  for (const menu of menus) {
    listen(menu.querySelector('summary'), 'click', () => {
      if (!menu.open) closeOthers(menu);
    });
    listen(menu, 'toggle', () => {
      if (menu.open) closeOthers(menu);
    });
  }
  listen(root, 'pointerdown', closeOutside);
  listen(root, 'click', closeOutside);
  listen(root, 'keydown', event => {
    if (event.key !== 'Escape') return;
    const opened = menus.filter(menu => menu.open);
    if (!opened.length) return;
    const current = opened.find(menu => menu.contains(event.target)) || opened.at(-1);
    closeOthers(null);
    event.preventDefault();
    current.querySelector('summary').focus();
  });
  return () => removeListeners.forEach(remove => remove());
}

export function mountGateTheme(root) {
  const form = root.querySelector('.gate-theme-toggle');
  if (!form) return () => {};
  const view = root.defaultView;
  const media = view.matchMedia('(prefers-color-scheme: dark)');
  const effectiveTheme = () => root.documentElement.dataset.theme === 'system'
    ? (media.matches ? 'dark' : 'light') : root.documentElement.dataset.theme;
  let previousFocus = null;
  const rememberFocus = () => { previousFocus = root.activeElement; };
  const toggle = event => {
    event.preventDefault();
    const theme = effectiveTheme() === 'dark' ? 'light' : 'dark';
    root.cookie = `mp_theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax${view.location.protocol === 'https:' ? '; Secure' : ''}`;
    root.documentElement.dataset.theme = theme;
    const languageTheme = root.querySelector('.preference input[name="theme"]');
    if (languageTheme) languageTheme.value = theme;
    const colorScheme = root.querySelector('meta[name="color-scheme"]');
    if (colorScheme) colorScheme.content = theme;
    if (previousFocus && previousFocus !== root.body && !form.contains(previousFocus)) previousFocus.focus({preventScroll:true});
    else form.querySelector(`.gate-theme-${theme === 'dark' ? 'light' : 'dark'}`)?.focus({preventScroll:true});
    previousFocus = null;
  };
  form.addEventListener('pointerdown', rememberFocus);
  form.addEventListener('submit', toggle);
  return () => {
    form.removeEventListener('pointerdown', rememberFocus);
    form.removeEventListener('submit', toggle);
  };
}

if (typeof document !== 'undefined') {
  mountGatePreferences(document);
  mountGateTheme(document);
}
