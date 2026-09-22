"""``python -m atlas_mcp <server>``: run one ATLAS MCP server on stdio (used by the Hermes profiles)."""

import sys

from .servers import SERVERS, ApiClient


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in SERVERS:
        sys.exit(f"usage: python -m atlas_mcp {{{','.join(SERVERS)}}}")
    SERVERS[args[0]](ApiClient()).run("stdio")


if __name__ == "__main__":
    main()
