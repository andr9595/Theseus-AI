(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.Continuation = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const TERMINAL = new Set(['complete', 'failed', 'cancelled']);

  function modeOf(run) {
    if (!run) return '';
    return run.mode || (run.solo ? 'solo' : 'council');
  }

  function titleOf(run) {
    const earlier = run && Array.isArray(run.conversation)
      ? run.conversation.filter(turn => turn && typeof turn === 'object')
      : [];
    const first = earlier.length ? earlier[0].task : run && run.task;
    return String(first || '').slice(0, 90);
  }

  /** Return the completed run that should be attached to the composer.
   *
   * This is deliberately a pure decision so reloads, commit-triggered state
   * refreshes and terminal events all apply exactly the same rules.
   */
  function target(run, view) {
    const options = view || {};
    if (!run || !run.file || !TERMINAL.has(run.state)) return null;
    if (options.busy || options.fresh || options.openChat) return null;

    const mode = modeOf(run);
    if (options.mode && mode !== options.mode) return null;

    const runWorkspace = String(run.workspace || run.repo || '');
    if (options.workspace && runWorkspace !== options.workspace) return null;

    return {
      file: run.file,
      task: titleOf(run),
      mode,
    };
  }

  return { modeOf, titleOf, target };
}));
