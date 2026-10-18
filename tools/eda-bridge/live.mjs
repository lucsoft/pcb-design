// Against the real editor, not a mock. These probes only mean anything
// with a browser on --remote-debugging-port and a logged-in editor, so
// they say which of the two is missing rather than just failing.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

let fail = 0;
const ok = (n, c, d = '') => { console.log(c ? `  ok    ${n}` : `  FAIL  ${n} ${d}`); if (!c) fail++; };

const srv = spawn(process.execPath, ['server.mjs'], { stdio: ['pipe', 'pipe', 'inherit'] });
const waiting = new Map();
createInterface({ input: srv.stdout }).on('line', (l) => {
  let m; try { m = JSON.parse(l); } catch { return; }
  waiting.get(m.id)?.(m); waiting.delete(m.id);
});
let id = 1;
const rpc = (method, params) => new Promise((r) => {
  const i = id++; waiting.set(i, r);
  srv.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: i, method, params }) + '\n');
});
const tool = async (name, args = {}) => {
  const r = await rpc('tools/call', { name, arguments: args });
  return { err: r.result?.isError, text: r.result?.content?.[0]?.text ?? '' };
};

await new Promise((r) => setTimeout(r, 300));
await rpc('initialize', {});

const st = JSON.parse((await tool('eda_status')).text);
if (!st.connected) {
  console.log(`\n  SKIP — ${st.reason}`);
  console.log('  Start the browser with --remote-debugging-port=9222,');
  console.log('  log in, and open the editor.');
  srv.kill(); process.exit(0);
}
ok('a live editor is reachable', st.connected, JSON.stringify(st));
ok('...and reports its version', /^\d+\.\d+/.test(String(st.version)), st.version);

const ns = JSON.parse((await tool('eda_namespaces')).text);
ok('the API namespaces are listable', Array.isArray(ns) && ns.length > 50, ns.length);
ok('...and include schematic, pcb and library',
  ['sch_PrimitiveComponent', 'pcb_PrimitiveComponent', 'lib_Device'].every((k) => ns.includes(k)));

const calls = JSON.parse((await tool('eda_namespaces', { namespace: 'sch_Netlist' })).text);
ok('a namespace lists its calls', calls.includes('getNetlist') && calls.includes('setNetlist'), calls);

const info = await tool('eda_editor_info');
ok('a named tool reaches the API', !info.err && /\d/.test(info.text), info.text.slice(0, 80));

const toast = await tool('eda_message', { text: 'pcb-design: live test' });
ok('a write reaches the editor (toast)', !toast.err, toast.text.slice(0, 80));

const bad = await tool('eda_call', { namespace: 'nope_Nothing', fn: 'x' });
ok('an unknown namespace is named, not swallowed',
  bad.err && /nope_Nothing/.test(bad.text), bad.text.slice(0, 90));

const badfn = await tool('eda_call', { namespace: 'sch_Netlist', fn: 'notAThing' });
ok('...and so is an unknown call', badfn.err && /notAThing/.test(badfn.text), badfn.text.slice(0, 90));

console.log();
console.log(fail ? `${fail} failure(s)` : 'all live bridge tests pass');
srv.kill(); process.exit(fail ? 1 : 0);
