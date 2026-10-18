#!/usr/bin/env node
/**
 * An MCP server that drives a live EasyEDA Pro editor.
 *
 * It talks to the editor through the Chrome DevTools Protocol rather
 * than through an extension. Why that is possible at all is argued in
 * `cdp.mjs` and measured in `README.md`: the editor keeps its whole
 * extension API on `window._EXTAPI_ROOT_` in the page's main world,
 * where CDP evaluates. No `.eext` to build, import, or keep in step with
 * the editor's version.
 *
 * The tools are a thin, *named* layer over that. `eda_call` can reach
 * anything the API has, and the named tools exist anyway, because a tool
 * list is what an agent reads to find out what it may do -- a single
 * do-anything call makes every schematic edit look alike to whoever is
 * granting permission.
 */

import { call, probe, evaluate, close } from './cdp.mjs';
import { createInterface } from 'node:readline';

// name, description, namespace, function, parameter schema, required
const TOOLS = [
  ['eda_status', 'Is a live EasyEDA Pro editor reachable, and which version?',
    null, null, {}],

  ['eda_editor_info', 'Editor version, and whether it is the web or desktop build.',
    'sys_Environment', 'getEditorCurrentVersion', {}],
  ['eda_project_info', 'The project currently open in the editor.',
    'dmt_Project', 'getCurrentProjectInfo', {}],
  ['eda_project_documents', "Every document in the editor's current project.",
    'dmt_Project', 'getCurrentProjectAllDocuments', {}],

  ['eda_schematic_components', 'Every component placed on the open schematic.',
    'sch_PrimitiveComponent', 'getAll', {}],
  ['eda_schematic_nets', 'Every net on the open schematic.',
    'sch_Net', 'getAll', {}],
  ['eda_schematic_netlist', 'The open schematic as a netlist.',
    'sch_Netlist', 'getNetlist', {}],
  ['eda_schematic_selected', 'What is selected on the open schematic.',
    'sch_SelectControl', 'getAllSelectedPrimitives', {}],
  ['eda_pcb_components', 'Every component placed on the open PCB.',
    'pcb_PrimitiveComponent', 'getAll', {}],

  ['eda_message', 'Show a toast in the editor. The cheapest proof the bridge is live.',
    'sys_Message', 'showToastMessage',
    { text: { type: 'string' } }, ['text']],

  // The escape hatch. Named tools cover what this project does often;
  // the API has 120+ namespaces and guessing which ones matter in
  // advance would be wrong. `eda_namespaces` makes it discoverable
  // rather than something you have to already know.
  ['eda_namespaces', 'List the API namespaces, or the calls in one of them.',
    null, null, { namespace: { type: 'string', description: 'omit to list all namespaces' } }],
  ['eda_call', 'Call any editor API: namespace, function, arguments.',
    null, null, {
      namespace: { type: 'string', description: 'e.g. sch_PrimitiveComponent' },
      fn: { type: 'string', description: 'e.g. getAll' },
      args: { type: 'array', description: 'arguments, JSON' },
    }, ['namespace', 'fn']],
];

const BY_NAME = new Map(TOOLS.map((t) => [t[0], t]));

async function run(name, a = {}) {
  if (name === 'eda_status') return { result: await probe() };

  if (name === 'eda_namespaces') {
    if (!a.namespace)
      return evaluate(
        'const r = window._EXTAPI_ROOT_;'
        + ' if (!r) throw new Error("extension API not loaded — log in and open the editor");'
        + ' return Object.keys(r).filter(k => /_/.test(k)).sort();');
    return evaluate(
      `const r = window._EXTAPI_ROOT_;
       if (!r) throw new Error("extension API not loaded — log in and open the editor");
       const ns = r[${JSON.stringify(a.namespace)}];
       if (!ns) throw new Error("no such namespace: " + ${JSON.stringify(a.namespace)});
       return Object.getOwnPropertyNames(Object.getPrototypeOf(ns))
         .filter(k => k !== "constructor").sort();`);
  }

  if (name === 'eda_call')
    return call(a.namespace, a.fn, Array.isArray(a.args) ? a.args : []);

  const t = BY_NAME.get(name);
  if (!t) return { error: `no such tool: ${name}` };
  const [, , ns, fn, props] = t;
  // Named tools pass their parameters positionally, in schema order --
  // the API takes positional arguments and an object would arrive as one
  // opaque blob.
  const args = Object.keys(props || {}).map((k) => a[k]).filter((v) => v !== undefined);
  return call(ns, fn, args);
}

// --- MCP, over stdio -------------------------------------------------------

const out = (id, result) =>
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');

async function handle(req) {
  if (req.method === 'initialize')
    return out(req.id, {
      protocolVersion: '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: { name: 'eda-bridge', version: '0.2.0' },
    });

  if (req.method === 'tools/list')
    return out(req.id, {
      tools: TOOLS.map(([name, description, , , props, required]) => ({
        name, description,
        inputSchema: {
          type: 'object',
          properties: props || {},
          ...(required ? { required } : {}),
        },
      })),
    });

  if (req.method === 'tools/call') {
    const r = await run(req.params?.name, req.params?.arguments ?? {});
    return out(req.id, {
      content: [{
        type: 'text',
        text: JSON.stringify(r.error ? { error: r.error } : r.result, null, 2),
      }],
      isError: Boolean(r.error),
    });
  }

  if (req.id !== undefined) out(req.id, {});
}

createInterface({ input: process.stdin }).on('line', (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch { return; }
  handle(req).catch((e) => {
    if (req.id !== undefined)
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0', id: req.id,
        error: { code: -32603, message: String(e?.message ?? e) },
      }) + '\n');
  });
});

process.on('exit', close);
process.stderr.write('eda-bridge: driving EasyEDA over CDP\n');
