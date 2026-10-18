---
type: Tool
title: "EasyEDA bridge: an MCP server and the extension it talks to"
description: Two halves of one bridge. The server runs here and listens; the extension runs inside EasyEDA Pro and dials in. What is tested, what is not, and why the shape is what it is.
status: draft
generated: { by: claude-code/claude-opus-5, at: 2026-10-18T10:00:00Z }
sources:
  - id: api
    resource: https://prodocs.easyeda.com/en/api/guide/
    title: The EasyEDA Pro extension API this drives
  - id: agent
    resource: https://github.com/easyeda/eext-easyeda-api-agent
    title: EasyEDA's own Apache-2.0 extension, which the manifest follows
---

# The EasyEDA bridge

## Why it is two halves and not one

`eda` is **not** a page global. The API documentation is explicit: every
extension gets its own `eda` object and it is not shared with any other.
So nothing outside an extension can reach it — not the browser console,
not the Chrome DevTools Protocol, not a userscript. Code that wants to
drive the editor has to *be* an extension.

That rules out the shape this would otherwise have taken. What is left is
a bridge: an extension inside the editor, a server outside it, and a
WebSocket between them.

**The editor dials out, the server listens.** An extension cannot open a
port, so the direction is forced — and it is the better one anyway. The
bridge is reachable only while somebody has opened the editor and picked
*Connect*; it does not stand open waiting.

**Desktop would not change any of this.** The desktop client is an
Electron wrapper around the same web application, with the same extension
isolation. It was considered and is not worth the NixOS packaging work:
EasyEDA ships Linux as a bare ZIP, and the Flathub build needs
`services.flatpak.enable` and a rebuild, for no capability the browser
does not already have.

## Installing it

**1. Register the server** in `.mcp.json`, beside `jlcpcb`:

```json
"eda-bridge": {
  "type": "stdio",
  "command": "/home/lucsoft/pcb-design/tools/eda-bridge.sh",
  "args": [], "env": {}
}
```

**2. Build the extension.** It needs EasyEDA's own SDK, which is not
vendored here:

```sh
cd tools/eda-bridge
npm install --no-save @jlceda/pro-api-types esbuild
npx esbuild src/index.ts --bundle --format=esm --outfile=dist/index.js
zip -r pcb-design-bridge.eext extension.json dist/
```

**3. Load it** in EasyEDA Pro: *Advanced → Extension Manager → Import*,
pick the `.eext`. Then *pcb-design bridge → Connect*. A toast says
whether it reached the server.

`eda_status` answers without an editor, so it is the first thing to call:
it reports whether one is connected and on which port.

## What the tools are

Pass-throughs, every one. The server owns no knowledge of schematics — it
forwards a named call and returns what came back. What a supply sheet
*is* lives in `tools/*.py`, where it can be tested without an editor in
the loop.

| tool | what it does |
|---|---|
| `eda_status` | is an editor connected, and on which port |
| `eda_editor_info` | editor and environment versions |
| `eda_document_info`, `eda_document_list` | the open document, and the project's documents |
| `eda_schematic_components`, `eda_schematic_nets`, `eda_schematic_selected` | what is on the open schematic |
| `eda_pcb_components` | what is on the open PCB |
| `eda_move_component`, `eda_rotate_component` | move or turn one schematic component |
| `eda_export_document`, `eda_export_3d` | export the document, or the board as STEP |
| `eda_show_message` | a toast, to prove the bridge is live |

**The write tools are deliberately two.** A schematic is a document
somebody is working in, and a bridge that can change anything can change
the wrong thing with no undo an agent can reach. The extension dispatches
through a **table of allowed calls** rather than `eda[ns][fn](...)`:
what is not listed cannot be invoked, and adding a capability is a diff
somebody can read.

## What is tested, and what is not

    cd tools/eda-bridge && npm test

Thirteen probes against a **mock editor**: that the server speaks MCP,
that a call reaches the editor by the name the extension registers, that
parameters travel with it, that the editor's own error is passed through
rather than swallowed, and that every way the editor can be absent —
never connected, disconnected mid-call, silent — produces an *error*
rather than a hang. An agent waiting forever on a dead editor is the
worst of the three answers, so each is a probe.

**What no test here can prove is that `eda.*` does what the documentation
says.** The call names in `src/index.ts` are written from the API
reference, not from a running editor — this machine has no EasyEDA, so
the bridge has never been attached to one. Expect some of them to be
wrong on first contact. `eda_editor_info` and `eda_show_message` are the
two to try first: they need no document, so a failure there is the bridge
and a failure elsewhere is a call name.

This is the same honesty the rest of the repository keeps about EasyEDA:
several questions in `CLAUDE.md` end in *"import it and look — that is
the only thing that does"*. Attaching the bridge is the first chance to
answer them.
