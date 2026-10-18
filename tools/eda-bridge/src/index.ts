/**
 * The half of the bridge that runs *inside* EasyEDA Pro.
 *
 * `eda` is not a page global. The documentation is explicit: every
 * extension gets its own `eda` object and it is not shared, so no amount
 * of browser automation reaches the API from outside -- not the console,
 * not the Chrome DevTools Protocol. Code that wants to drive the editor
 * has to *be* an extension, which is what this file is.
 *
 * It is deliberately thin. It owns no knowledge of the board, keeps no
 * state beyond the socket, and decides nothing: it accepts a call by
 * name, looks it up in a table of what is allowed, runs it and sends the
 * result back. Everything that knows what a supply sheet is lives on the
 * other side, in the repository, where it can be read and tested.
 *
 * The socket is outbound to localhost. An extension cannot listen, and
 * that is the right way round anyway -- the editor connects to the
 * toolchain when a person asks it to, rather than standing open.
 */

const DEFAULT_URL = 'ws://127.0.0.1:18761';

let socket: any = null;
let url = DEFAULT_URL;

/** A call this bridge will make, and nothing else.
 *
 * A name-to-function table rather than `eda[ns][fn](...)`. Reaching into
 * the API by string is one typo away from calling something nobody
 * intended, and the editor has no undo for an agent -- so what is not
 * listed here cannot be invoked, and adding a capability is a diff
 * somebody can read.
 */
const CALLS: Record<string, (p: any) => Promise<unknown>> = {
	// --- read ------------------------------------------------------------
	'editor.info': async () => ({
		editor: await eda.sys_Environment.getEditorInfo?.(),
		env: await eda.sys_Environment.getEnvironmentInfo?.(),
	}),
	'doc.info': async () => eda.dmt_Document?.getCurrentDocumentInfo?.(),
	'doc.list': async () => eda.dmt_Project?.getCurrentProjectAllDocuments?.(),
	'sch.components': async () =>
		eda.sch_PrimitiveComponent?.getAll?.(),
	'sch.selected': async () =>
		eda.sch_SelectControl?.getAllSelectedPrimitives?.(),
	'sch.nets': async () => eda.sch_Net?.getAll?.(),
	'pcb.components': async () => eda.pcb_PrimitiveComponent?.getAll?.(),

	// --- write -------------------------------------------------------------
	// Narrow on purpose. A schematic is a document somebody is working in,
	// and a tool that can move anything can move the wrong thing silently.
	'sch.move': async (p) =>
		eda.sch_PrimitiveComponent?.modify?.(p.id, { x: p.x, y: p.y }),
	'sch.rotate': async (p) =>
		eda.sch_PrimitiveComponent?.modify?.(p.id, { rotation: p.rotation }),

	// --- export ------------------------------------------------------------
	'export.document': async (p) =>
		eda.dmt_Document?.exportDocument?.(p.type, p.path),
	'export.3d': async (p) => eda.pcb_Export?.export3DFile?.(p.type ?? 'STEP'),

	// --- say something, so a person can tell the bridge is alive -----------
	'ui.message': async (p) => eda.sys_Message?.showToastMessage?.(p.text),
};

function send(payload: unknown) {
	try {
		socket?.send(JSON.stringify(payload));
	} catch (e) {
		/* the socket went away between the call and the reply */
	}
}

async function dispatch(msg: any) {
	const fn = CALLS[msg?.call];
	if (!fn) {
		// Naming what was asked for, not just refusing: "unknown call" with
		// no name sends the reader to grep the whole table.
		send({ id: msg?.id, error: `no such call: ${msg?.call}` });
		return;
	}
	try {
		send({ id: msg.id, result: (await fn(msg.params ?? {})) ?? null });
	} catch (e: any) {
		send({ id: msg.id, error: String(e?.message ?? e) });
	}
}

export function connect(at?: string) {
	if (socket) {
		eda.sys_Message?.showToastMessage?.('bridge: already connected');
		return;
	}
	url = at || DEFAULT_URL;
	socket = new eda.sys_WebSocket.WebSocket(url);
	socket.onOpen(() => {
		send({ hello: 'pcb-design bridge', version: '0.1.0' });
		eda.sys_Message?.showToastMessage?.(`bridge: connected to ${url}`);
	});
	socket.onMessage((raw: string) => {
		let msg: any;
		try {
			msg = JSON.parse(raw);
		} catch {
			send({ error: 'not JSON' });
			return;
		}
		dispatch(msg);
	});
	socket.onClose(() => {
		socket = null;
		eda.sys_Message?.showToastMessage?.('bridge: disconnected');
	});
	socket.onError(() => {
		socket = null;
		eda.sys_Message?.showToastMessage?.(`bridge: cannot reach ${url}`);
	});
}

export function disconnect() {
	socket?.close?.();
	socket = null;
}

export function status() {
	eda.sys_Message?.showToastMessage?.(
		socket ? `bridge: connected to ${url}` : 'bridge: not connected',
	);
}

export function activate() {
	/* nothing on load: the bridge opens when a person asks it to */
}
