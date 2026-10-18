#!/usr/bin/env node
/**
 * Protocol tests for the bridge, against a mock editor.
 *
 * What this can prove without EasyEDA: that the server speaks MCP, that
 * a tool call reaches the editor and its answer comes back, and that
 * every way the editor can be absent produces an error rather than a
 * hang. What it cannot prove is that `eda.sch_PrimitiveComponent.getAll`
 * returns what the documentation says -- that needs an editor, and the
 * README says so rather than leaving it implied.
 */

import { spawn } from 'node:child_process';
import { WebSocket } from 'ws';
import { createInterface } from 'node:readline';

const PORT = 18799;
let failures = 0;

function check(name, cond, detail = '') {
	if (cond) console.log(`  ok    ${name}`);
	else {
		console.log(`  FAIL  ${name} ${detail}`);
		failures++;
	}
}

const srv = spawn(process.execPath, ['server.mjs'], {
	env: { ...process.env, EDA_BRIDGE_PORT: String(PORT) },
	stdio: ['pipe', 'pipe', 'pipe'],
});

const out = createInterface({ input: srv.stdout });
const waiting = new Map();
out.on('line', (l) => {
	let m;
	try { m = JSON.parse(l); } catch { return; }
	const slot = waiting.get(m.id);
	if (slot) { waiting.delete(m.id); slot(m); }
});

let rpcId = 1;
function rpc(method, params) {
	const id = rpcId++;
	return new Promise((res) => {
		waiting.set(id, res);
		srv.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
	});
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
	await sleep(300);

	const init = await rpc('initialize', {});
	check('the server speaks MCP', init.result?.serverInfo?.name === 'eda-bridge',
		JSON.stringify(init.result));

	const list = await rpc('tools/list', {});
	const names = (list.result?.tools ?? []).map((t) => t.name);
	check('it offers tools', names.length > 10, names.length);
	check('...including a write and an export',
		names.includes('eda_move_component') && names.includes('eda_export_3d'), names);
	check('...and every one has a schema',
		(list.result?.tools ?? []).every((t) => t.inputSchema?.type === 'object'));

	// With no editor, a call must *fail*, not hang. An agent waiting
	// forever on a disconnected editor is the worst of the three answers.
	const cold = await rpc('tools/call', { name: 'eda_document_info', arguments: {} });
	check('with no editor, a call errors rather than hanging',
		cold.result?.isError === true &&
		/no editor connected/.test(cold.result.content[0].text),
		cold.result?.content?.[0]?.text);

	check('...and status says so without needing one',
		JSON.parse((await rpc('tools/call',
			{ name: 'eda_status', arguments: {} })).result.content[0].text)
			.connected === false);

	// A mock editor, answering the way the extension does.
	const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
	const seen = [];
	await new Promise((r) => ws.on('open', r));
	ws.on('message', (raw) => {
		const m = JSON.parse(String(raw));
		seen.push(m);
		if (m.call === 'doc.info') ws.send(JSON.stringify({ id: m.id, result: { name: 'supply' } }));
		else if (m.call === 'sch.move') ws.send(JSON.stringify({ id: m.id, error: 'locked' }));
		// `ui.message` is answered by nobody, to exercise the timeout.
	});
	await sleep(200);

	check('a connected editor shows as connected',
		JSON.parse((await rpc('tools/call',
			{ name: 'eda_status', arguments: {} })).result.content[0].text)
			.connected === true);

	const doc = await rpc('tools/call', { name: 'eda_document_info', arguments: {} });
	check('a call reaches the editor and the answer comes back',
		JSON.parse(doc.result.content[0].text).name === 'supply',
		doc.result?.content?.[0]?.text);
	check('...by the call name the extension registers',
		seen.some((m) => m.call === 'doc.info'), seen.map((m) => m.call));

	const mv = await rpc('tools/call',
		{ name: 'eda_move_component', arguments: { id: 'x', x: 1, y: 2 } });
	check("the editor's own error is passed through, not swallowed",
		mv.result?.isError === true && /locked/.test(mv.result.content[0].text),
		mv.result?.content?.[0]?.text);
	check('...and the parameters travel with the call',
		seen.some((m) => m.call === 'sch.move' && m.params?.x === 1),
		JSON.stringify(seen.find((m) => m.call === 'sch.move')));

	const unknown = await rpc('tools/call', { name: 'eda_nonsense', arguments: {} });
	check('an unknown tool is named, not just refused',
		unknown.result?.isError === true &&
		/eda_nonsense/.test(unknown.result.content[0].text));

	// The editor going away mid-call must fail the call.
	const hang = rpc('tools/call', { name: 'eda_show_message', arguments: { text: 'hi' } });
	await sleep(150);
	ws.close();
	const after = await hang;
	check('an editor that disconnects fails the call in flight',
		after.result?.isError === true &&
		/disconnected/.test(after.result.content[0].text),
		after.result?.content?.[0]?.text);

	console.log();
	console.log(failures ? `${failures} failure(s)` : 'all bridge tests pass');
	srv.kill();
	process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); srv.kill(); process.exit(1); });
