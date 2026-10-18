/**
 * Talking to EasyEDA Pro over the Chrome DevTools Protocol.
 *
 * **This replaces the extension, and the reason is measured rather than
 * assumed.** The API documentation says every extension gets its own
 * `eda` object, "not shared with other extensions", which reads as
 * isolation no outside caller can cross -- and that is how this bridge
 * was first built. It is wrong, or at least not the whole truth: the
 * editor keeps the same object graph on `window._EXTAPI_ROOT_`, in the
 * page's main world, where CDP evaluates. Probed on a live editor:
 *
 *     _EXTAPI_ROOT_ -> sch_*, pcb_*, dmt_*, lib_*, sys_* (120+ namespaces)
 *     sys_Environment.getEditorCurrentVersion() -> "3.2.206"
 *     sys_Message.showToastMessage(...)         -> a toast appears
 *
 * So no `.eext` has to be built, imported, or kept in step with the
 * editor's version. The cost is that the browser must run with
 * `--remote-debugging-port`, which is a real exposure and is said plainly
 * in the README rather than buried.
 *
 * `await` is not valid at the top level of `Runtime.evaluate`, so every
 * call is wrapped in an async IIFE -- the first attempt returned
 * "SyntaxError: missing ) after argument list" and looked like a broken
 * API rather than a broken wrapper.
 */

import { WebSocket } from 'ws';

const DEFAULT_CDP = process.env.EDA_CDP_URL || 'http://127.0.0.1:9222';

let ws = null;
let nextId = 1;
const pending = new Map();

async function findEditor(base) {
  let list;
  try {
    const r = await fetch(`${base}/json/list`, { signal: AbortSignal.timeout(3000) });
    list = await r.json();
  } catch {
    return { error: `no browser on ${base}. Start it with --remote-debugging-port=9222` };
  }
  const page = list.find(
    (t) => t.type === 'page' && /pro\.easyeda\.com|pro\.lceda\.cn/i.test(t.url),
  );
  if (!page) return { error: 'no EasyEDA Pro tab open in that browser' };
  return { page };
}

async function attach(base = DEFAULT_CDP) {
  if (ws?.readyState === 1) return {};
  const { page, error } = await findEditor(base);
  if (error) return { error };
  ws = new WebSocket(page.webSocketDebuggerUrl);
  const ok = await new Promise((res) => {
    ws.once('open', () => res(true));
    ws.once('error', () => res(false));
  });
  if (!ok) { ws = null; return { error: 'could not attach to the tab' }; }
  ws.on('message', (raw) => {
    let d;
    try { d = JSON.parse(String(raw)); } catch { return; }
    const slot = pending.get(d.id);
    if (slot) { pending.delete(d.id); slot(d); }
  });
  ws.on('close', () => {
    ws = null;
    // Fail everything in flight. A call that never returns reads as a
    // hung agent; "the tab went away" is the answer worth having.
    for (const [, slot] of pending) slot({ __gone: true });
    pending.clear();
  });
  return {};
}

/** Run JavaScript in the editor's page and return its value. */
export async function evaluate(body, { timeoutMs = 20000, base } = {}) {
  const a = await attach(base);
  if (a.error) return a;
  const id = nextId++;
  const expr = `(async () => { ${body} })()`;
  const d = await new Promise((res) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      res({ __timeout: true });
    }, timeoutMs);
    pending.set(id, (m) => { clearTimeout(timer); res(m); });
    ws.send(JSON.stringify({
      id, method: 'Runtime.evaluate',
      params: { expression: expr, returnByValue: true, awaitPromise: true },
    }));
  });
  if (d.__timeout) return { error: `timed out after ${timeoutMs} ms` };
  if (d.__gone) return { error: 'the editor tab closed' };
  if (d.result?.exceptionDetails) {
    const e = d.result.exceptionDetails;
    return {
      error: String(e.exception?.description || e.text).split('\n')[0],
    };
  }
  return { result: d.result?.result?.value ?? null };
}

/** Is the API object actually present? Three answers, not two. */
export async function probe(base) {
  const a = await attach(base);
  if (a.error) return { connected: false, reason: a.error };
  const r = await evaluate(
    'return typeof _EXTAPI_ROOT_ === "undefined" ? null :'
    + ' { version: await _EXTAPI_ROOT_.sys_Environment.getEditorCurrentVersion(),'
    + '   web: await _EXTAPI_ROOT_.sys_Environment.isWeb() };',
  );
  if (r.error) return { connected: false, reason: r.error };
  if (r.result === null)
    return {
      connected: false,
      reason: 'tab is open but the extension API is not loaded — log in to'
        + ' EasyEDA and open the editor',
    };
  return { connected: true, ...r.result };
}

/** `eda.<ns>.<fn>(...args)`, by name. */
export async function call(ns, fn, args = [], opts = {}) {
  const a = JSON.stringify(args);
  return evaluate(
    `const root = window._EXTAPI_ROOT_;
     if (!root) throw new Error("extension API not loaded — log in and open the editor");
     const ns = root[${JSON.stringify(ns)}];
     if (!ns) throw new Error("no such namespace: " + ${JSON.stringify(ns)});
     const fn = ns[${JSON.stringify(fn)}];
     if (typeof fn !== "function") throw new Error("no such call: " + ${JSON.stringify(ns + '.' + fn)});
     const out = await fn.apply(ns, ${a});
     return out === undefined ? null : JSON.parse(JSON.stringify(out));`,
    opts,
  );
}

export function close() { try { ws?.close(); } catch {} ws = null; }
