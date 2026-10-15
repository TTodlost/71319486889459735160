/* A failed fetch never replaces the most recent usable published snapshot. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BattlecodeSnapshot = factory();
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  function createSnapshotSource({url, validate, fetcher = globalThis.fetch.bind(globalThis), now = Date.now}) {
    let current = null, pending = null;
    return {
      get current() { return current; },
      reload() {
        if (pending) return pending;
        pending = (async () => {
          const target = new URL(url);
          target.searchParams.set('v', String(now()));
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 30000);
          try {
            const response = await fetcher(target.href, {cache: 'no-store', credentials: 'omit', signal: controller.signal});
            if (!response.ok) throw new Error(`Snapshot request failed (${response.status}).`);
            let candidate;
            try { candidate = await response.json(); }
            catch { throw new Error('The published snapshot could not be read.'); }
            validate(candidate);
            current = candidate;
            return current;
          } finally { clearTimeout(timeout); }
        })().finally(() => { pending = null; });
        return pending;
      }
    };
  }
  return {createSnapshotSource};
});
