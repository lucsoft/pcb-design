---
type: Tool
title: "EasyEDA bridge: driving a live editor over CDP"
description: An MCP server that reaches the EasyEDA Pro extension API through the browser's debugging protocol. What was measured to make that possible, what it costs, and what is not proven.
status: draft
generated: { by: claude-code/claude-opus-5, at: 2026-10-18T10:30:00Z }
verified:
  - { by: process:live-test, at: 2026-10-18T10:30:00Z }
sources:
  - id: api
    resource: https://prodocs.easyeda.com/en/api/guide/
    title: The EasyEDA Pro extension API this drives
---

# The EasyEDA bridge

    nix-shell --run 'cd tools/eda-bridge && node live.mjs'

## Why there is no extension

The API documentation says every extension gets its own `eda` object,
"not shared with other extensions". That reads as isolation nothing
outside can cross, and this bridge was first built the way that implies:
an `.eext` inside the editor, a server outside, a WebSocket between.

**Measured on a live editor, it is not true** — or not the whole truth.
The editor keeps the same object graph on `window._EXTAPI_ROOT_`, in the
page's main world, which is exactly where the Chrome DevTools Protocol
evaluates:

    _EXTAPI_ROOT_                              120+ namespaces
      sch_PrimitiveComponent, sch_Netlist, …   schematic
      pcb_PrimitiveComponent, pcb_Drc, …       board
      dmt_Project, lib_Device, sys_*           project, library, system

    sys_Environment.getEditorCurrentVersion()  "3.2.206"
    sys_Message.showToastMessage(…)            a toast appears

So the extension is gone and nothing is lost: no `.eext` to build, no
import step, nothing to keep in step with the editor's version.

**Two things about this were wrong before they were checked**, and both
are the same mistake — reasoning from the documentation instead of
probing. The first was the isolation. The second was the conclusion drawn
from it, that the desktop client might help; it is an Electron wrapper
around the same page and would have changed nothing.

## What it costs, plainly

The browser has to run with `--remote-debugging-port=9222`. **That port
is full control of every tab in that browser** — cookies, sessions,
anything typed. Any local process can use it. This is a real exposure and
the reason it is stated here rather than in a footnote.

    helium --remote-debugging-port=9222        # or any Chromium build

The server finds the EasyEDA tab itself; it does not open or close tabs,
and it never navigates.

## The tools

`eda_status` first, always. It answers without an editor and gives three
distinct answers rather than two: no browser on the port, a browser but
no EasyEDA tab, or a tab whose API has not loaded because nobody is
logged in. Each needs a different fix, so each says so.

| tool | |
|---|---|
| `eda_status` | is a live editor reachable, and which version |
| `eda_namespaces` | the API's namespaces, or the calls in one |
| `eda_call` | any namespace, function and arguments |
| `eda_editor_info` | editor version and build |
| `eda_project_info`, `eda_project_documents` | the open project |
| `eda_schematic_components`, `_nets`, `_netlist`, `_selected` | the open schematic |
| `eda_pcb_components` | the open board |
| `eda_message` | a toast — the cheapest proof the bridge is live |

**`eda_call` can reach anything, and the named tools exist anyway.** A
tool list is what an agent reads to find out what it may do; a single
do-anything call makes every schematic edit look alike to whoever is
granting permission. `eda_namespaces` is what makes the rest
discoverable, because guessing in advance which of 120 namespaces matter
would have been wrong.

## What is proven and what is not

`live.mjs` runs nine probes against a **real editor**: that it is
reachable and reports a version, that the namespaces list and contain
the expected ones, that a namespace lists its calls, that a named tool
reaches the API, that a write arrives (the toast), and that an unknown
namespace or call is **named** in the error rather than swallowed. All
nine pass against EasyEDA Pro 3.2.206 in Helium.

It skips cleanly with the reason when no editor is up, rather than
failing — a test that cannot run is not a test that failed.

**Not proven:** anything that modifies a document. The write path is
`eda_call` plus whatever the API offers, and the only write exercised
here is a toast. `sch_Netlist.setNetlist` and `sch_PrimitiveComponent.create`
exist and are the obvious way to close the import loop that `CLAUDE.md`
still describes as a manual step — but neither has been run, and running
one against a project somebody cares about is a decision for a person,
not a default.
