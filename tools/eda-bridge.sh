#!/usr/bin/env bash
# Launcher for the EasyEDA bridge MCP server.
#
# node only exists inside the project's nix-shell, so the server cannot be
# registered as a bare command -- same reason `jlcpcb-mcp.sh` exists.
#
# The server listens on 127.0.0.1 and the *editor* dials in, because an
# extension cannot listen on a port. That direction is also the safer
# one: the bridge is reachable only while somebody has opened the editor
# and picked Connect, rather than standing open.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
exec nix-shell "$here/../shell.nix" --run "exec node '$here/eda-bridge/server.mjs'"
