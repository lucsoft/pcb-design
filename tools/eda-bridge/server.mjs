#!/usr/bin/env node
/**
 * The half of the bridge that runs outside EasyEDA: an MCP server on
 * stdio, and a WebSocket the editor connects back to.
 *
 * The direction matters. An extension cannot listen on a port, so this
 * side listens and the editor dials in when a person picks
 * `pcb-design bridge -> Connect`. That also means the editor is only
 * reachable while somebody has opened it on purpose, which is the right
 * default for a tool that can move parts in a document.
 *
 * Every tool here is a pass-through. The server owns no knowledge of
 * schematics; it forwards a named call and returns what came back. What
 * a supply sheet *is* lives in the repository, in Python, where it can
 * be tested without an editor in the loop.
 */

import { WebSocketServer } from 'ws';
import { createInterface } from 'node:readline';

const PORT = Number(process.env.EDA_BRIDGE_PORT || 18761);

// --- the editor side -------------------------------------------------------

let editor = null;
let nextId = 1;
const pending = new Map();

const wss = new WebSocketServer({ host: '127.0.0.1', port: PORT });
wss.on('connection', (ws) => {
	// One editor at a time. Two would make "the current document"
	// ambiguous, and a tool whose target depends on connection order is
	// one nobody can reason about.
	if (editor) ws.close(1013, 'bridge already has an editor');
	else {
		editor = ws;
		ws.on('message', (raw) => {
			let msg;
			try {
				msg = JSON.parse(String(raw));
			} catch {
				return;
			}
			const slot = pending.get(msg.id);
			if (!slot) return;          // a hello, or a reply to a dead call
			pending.delete(msg.id);
			slot(msg);
		});
		ws.on('close', () => {
			editor = null;
			// Fail every call in flight rather than leaving it hanging: a
			// tool that never returns reads as a hung agent, and the real
			// answer -- the editor went away -- is the useful one.
			for (const [, slot] of pending) slot({ error: 'editor disconnected' });
			pending.clear();
		});
	}
});

function callEditor(call, params, timeoutMs = 20000) {
	return new Promise((resolve) => {
		if (!editor) {
			resolve({
				error:
					'no editor connected. In EasyEDA Pro: pcb-design bridge -> Connect',
			});
			return;
		}
		const id = nextId++;
		const timer = setTimeout(() => {
			pending.delete(id);
			resolve({ error: `timed out after ${timeoutMs} ms` });
		}, timeoutMs);
		pending.set(id, (msg) => {
			clearTimeout(timer);
			resolve(msg);
		});
		editor.send(JSON.stringify({ id, call, params }));
	});
}

// --- the tools -------------------------------------------------------------

const TOOLS = [
	['eda_status', 'Is an EasyEDA editor connected to the bridge?', null, {}],
	['eda_editor_info', 'Editor and environment version information.', 'editor.info', {}],
	['eda_document_info', 'The document currently open in the editor.', 'doc.info', {}],
	['eda_document_list', "Every document in the editor's current project.", 'doc.list', {}],
	['eda_schematic_components', 'Every component placed on the open schematic.', 'sch.components', {}],
	['eda_schematic_selected', 'What is selected on the open schematic.', 'sch.selected', {}],
	['eda_schematic_nets', 'Every net on the open schematic.', 'sch.nets', {}],
	['eda_pcb_components', 'Every component placed on the open PCB.', 'pcb.components', {}],
	['eda_move_component', 'Move one schematic component to a coordinate.', 'sch.move', {
		id: { type: 'string', description: 'the component primitive id' },
		x: { type: 'number' }, y: { type: 'number' },
	}, ['id', 'x', 'y']],
	['eda_rotate_component', 'Rotate one schematic component.', 'sch.rotate', {
		id: { type: 'string' },
		rotation: { type: 'number', description: '0, 90, 180 or 270' },
	}, ['id', 'rotation']],
	['eda_export_document', 'Export the open document to a file type.', 'export.document', {
		type: { type: 'string', description: 'e.g. PDF, JSON' },
		path: { type: 'string', description: 'where to write it' },
	}, ['type']],
	['eda_export_3d', 'Export the board as a 3D model.', 'export.3d', {
		type: { type: 'string', description: 'STEP by default' },
	}],
	['eda_show_message', 'Show a toast in the editor, to prove the bridge is live.', 'ui.message', {
		text: { type: 'string' },
	}, ['text']],
];

function schema(props, required) {
	return {
		type: 'object',
		properties: props || {},
		...(required ? { required } : {}),
	};
}

const BY_NAME = new Map(TOOLS.map((t) => [t[0], t]));

// --- MCP, over stdio -------------------------------------------------------

function reply(id, result) {
	process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
}

async function handle(req) {
	if (req.method === 'initialize') {
		reply(req.id, {
			protocolVersion: '2024-11-05',
			capabilities: { tools: {} },
			serverInfo: { name: 'eda-bridge', version: '0.1.0' },
		});
		return;
	}
	if (req.method === 'tools/list') {
		reply(req.id, {
			tools: TOOLS.map(([name, description, , props, required]) => ({
				name,
				description,
				inputSchema: schema(props, required),
			})),
		});
		return;
	}
	if (req.method === 'tools/call') {
		const t = BY_NAME.get(req.params?.name);
		if (!t) {
			reply(req.id, {
				content: [{ type: 'text', text: `no such tool: ${req.params?.name}` }],
				isError: true,
			});
			return;
		}
		const [, , call] = t;
		const out = call === null
			? { result: { connected: Boolean(editor), port: PORT } }
			: await callEditor(call, req.params?.arguments ?? {});
		reply(req.id, {
			content: [{
				type: 'text',
				text: JSON.stringify(out.error ? { error: out.error } : out.result, null, 2),
			}],
			isError: Boolean(out.error),
		});
		return;
	}
	if (req.id !== undefined) reply(req.id, {});
}

createInterface({ input: process.stdin }).on('line', (line) => {
	if (!line.trim()) return;
	let req;
	try {
		req = JSON.parse(line);
	} catch {
		return;
	}
	handle(req).catch((e) => {
		if (req.id !== undefined)
			process.stdout.write(JSON.stringify({
				jsonrpc: '2.0', id: req.id,
				error: { code: -32603, message: String(e?.message ?? e) },
			}) + '\n');
	});
});

process.stderr.write(`eda-bridge: waiting for an editor on ws://127.0.0.1:${PORT}\n`);
