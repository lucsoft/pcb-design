# Development shell for the PCB design workflow.
#
# Uses <nixpkgs> from NIX_PATH so it follows the same npins-managed pin as the
# rest of the system. No flakes, no channels.
#
#   nix-shell            # interactive
#   nix-shell --run ...  # one-shot
{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShell {
  name = "pcb-design";

  packages = with pkgs; [
    # ERC checker and knowledge-base tooling.
    (python3.withPackages (ps: with ps; [ pyyaml jsonschema matplotlib ]))

    # Datasheet extraction. pdftotext/pdfimages/pdfinfo come from poppler,
    # mupdf adds mutool as a fallback for PDFs poppler mangles.
    poppler-utils
    mupdf

    # npx, for the JLCPCB/LCSC MCP server.
    nodejs

    jq
    curl
  ];

  shellHook = ''
    export PCB_ROOT="$PWD"
    export PCB_KB="$PWD/kb"

    # The banner goes to stderr on purpose. This shell also wraps the MCP
    # server, and anything written to stdout corrupts its JSON-RPC stream.
    echo "pcb-design shell — python $(python3 --version | cut -d' ' -f2), node $(node --version), pdftotext $(pdftotext -v 2>&1 | head -1 | cut -d' ' -f3)" >&2
  '';
}
