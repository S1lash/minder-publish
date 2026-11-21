const SNAPSHOT_MAX_AGE_MS = 30_000;

export function mountCapacity({root, t, bytes, getSettings, refreshSettings}) {
  const summary = root.querySelector('summary');
  const gauge = root.querySelector('.capacity-fill');
  const percent = root.querySelector('.capacity-percent');
  const refresh = root.querySelector('[data-capacity="refresh"]');
  const status = root.querySelector('[data-capacity="status"]');
  const updated = root.querySelector('[data-capacity="updated"]');
  let lastSettings;
  let updatedAt = 0;
  let invalidated = false;
  let revision = 0;
  let refreshing = false;
  const values = Object.fromEntries(['storage', 'records', 'reserved'].map(key =>
    [key, root.querySelector(`[data-capacity="${key}"]`)]
  ));

  function render(settings) {
    const storage = settings?.storage;
    const maximum = settings?.limits?.publication_count;
    const valid = storage && [storage.used_bytes, storage.quota_bytes,
      storage.reserved_bytes, storage.publication_count, maximum].every(Number.isFinite);
    root.hidden = !valid;
    if (settings !== lastSettings) {
      lastSettings = settings;
      updatedAt = valid ? Date.now() : 0;
      invalidated = false;
      status.hidden = true;
      revision++;
    }
    if (!valid) {
      root.open = false;
      status.hidden = true;
      return;
    }

    const ratio = (used, total) => total > 0 ? used / total : 1;
    const effective = Math.max(ratio(storage.used_bytes, storage.quota_bytes),
      ratio(storage.publication_count, maximum));
    const displayedPercent = Math.round(effective * 100);
    const language = document.documentElement.lang;
    const number = new Intl.NumberFormat(language);
    const percentage = new Intl.NumberFormat(language, {style: 'percent'});
    const formatted = displayedPercent > 999 ? `${percentage.format(9.99)}+` :
      percentage.format(displayedPercent / 100);

    percent.textContent = formatted;
    gauge.setAttribute('stroke-dasharray', `${Math.min(100, effective * 100)} 100`);
    summary.setAttribute('aria-label', `${formatted} ${t('Usage')}`);
    summary.title = summary.getAttribute('aria-label');
    values.storage.textContent = `${bytes(storage.used_bytes)} / ${bytes(storage.quota_bytes)}`;
    values.records.textContent = `${number.format(storage.publication_count)} / ${number.format(maximum)}`;
    values.reserved.textContent = bytes(storage.reserved_bytes);
    const time = new Intl.DateTimeFormat(language, {timeStyle: 'short'}).format(updatedAt);
    updated.textContent = t('Updated {time}', {time});
  }

  async function reload() {
    if (refreshing || root.hidden) return;
    refreshing = true;
    const current = revision;
    refresh.setAttribute('aria-disabled', 'true');
    status.hidden = true;
    try {
      await refreshSettings(() => current === revision);
      render(getSettings());
    } catch {
      if (root.hidden || current !== revision) return;
      status.textContent = t('Could not refresh capacity.');
      status.hidden = false;
    } finally {
      refresh.removeAttribute('aria-disabled');
      refreshing = false;
    }
  }

  refresh.addEventListener('click', reload);
  root.addEventListener('toggle', () => {
    if (root.open && (invalidated || Date.now() - updatedAt >= SNAPSHOT_MAX_AGE_MS)) {
      void reload();
    }
  });

  function dismissOutside(event) {
    if (root.open && !root.contains(event.target)) root.open = false;
  }
  document.addEventListener('pointerdown', dismissOutside);
  document.addEventListener('focusin', event => {
    if (!event.target.contains(root)) dismissOutside(event);
  });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape' && root.open) {
      event.preventDefault();
      root.open = false;
      summary.focus();
    }
  });
  return {
    render,
    invalidate() {
      invalidated = true;
      revision++;
    },
  };
}
