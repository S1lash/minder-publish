/* Renewal follows visible owner actions; a passive open tab never keeps a session alive. */
function expiryMilliseconds(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value * 1000;
  return typeof value === 'string' ? Date.parse(value) : NaN;
}

export function mountSessionActivity({getSession, refresh, onError = () => {}, document = globalThis.document, clock = Date.now}) {
  if (typeof getSession !== 'function' || typeof refresh !== 'function' || !document?.addEventListener) {
    throw new TypeError('Session activity requires session, refresh and document interfaces.');
  }
  let disposed = false;
  let observed = null;
  let lastAttempt = -Infinity;
  let inFlight = null;
  const events = ['pointerdown', 'click', 'keydown', 'change'];
  const options = {capture: true, passive: true};

  async function activity(event) {
    if (disposed || !event.isTrusted || event.repeat || document.visibilityState !== 'visible') return;
    if (event.target?.closest?.('[data-session-no-refresh]')) return;
    const session = getSession();
    if (!session || session.authenticated !== true) return;
    const instant = clock();
    const interval = session.refresh_interval_seconds;
    if (typeof interval !== 'number' || !Number.isFinite(interval) || interval <= 0) return;
    if (!(expiryMilliseconds(session.expires_at) > instant) || !(expiryMilliseconds(session.absolute_expires_at) > instant)) return;
    if (observed !== session) {
      observed = session;
      lastAttempt = -Infinity;
    }
    if (inFlight?.session === session) return;
    if (instant >= lastAttempt && instant - lastAttempt < interval * 1000) return;
    lastAttempt = instant;
    const request = {session};
    inFlight = request;
    try {
      const result = await refresh();
      if (!disposed && getSession() === session && session.authenticated === true && result && typeof result === 'object' && !Array.isArray(result)) {
        Object.assign(session, result);
      }
    } catch (error) {
      // A failed request from an old login must not sign out its replacement.
      if (!disposed && getSession() === session && session.authenticated === true) onError(error);
    } finally {
      if (inFlight === request) inFlight = null;
    }
  }

  events.forEach(name => document.addEventListener(name, activity, options));
  return () => {
    disposed = true;
    events.forEach(name => document.removeEventListener(name, activity, options));
    observed = null;
  };
}
