#!/usr/bin/env bash
# Launcher for the JLCPCB/LCSC MCP server.
#
# node only exists inside the project's nix-shell, so the server cannot be
# registered as a bare `npx` command. This wrapper supplies the interpreter.
#
# No JLCPCB API credentials are set here on purpose. Without them the server
# exposes search, specs, stock, pricing and datasheet lookup, while every
# ordering tool (jlcpcb_pcb_create_order, jlcpcb_tdp_create_order) fails
# closed. Those place real, paid orders — add credentials only when you
# actually intend to enable that, and know that it lets an agent spend money.
set -euo pipefail
exec nix-shell "$(dirname "$0")/../shell.nix" --run 'exec npx -y jlcpcb-mcp' 
