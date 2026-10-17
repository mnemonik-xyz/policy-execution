"""The warrant-buyer command: set up the buyer guardrail, check the wallet, and run the guardrail.

    warrant-buyer init        write the wallet, the keys and the mainnet manifest
    warrant-buyer status      show the wallet address and its USDC balance
    warrant-buyer guardrail   run the guardrail MCP server (the MCP client starts this)
"""

import sys

USAGE = __doc__.split("\n\n", 1)[1]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args else ""

    if command == "init":
        from .buyer_init import main as init_main

        return init_main(args, prog="warrant-buyer init")
    if command == "status":
        from .buyer_init import main as init_main

        return init_main([*args, "--status"], prog="warrant-buyer status")
    if command == "guardrail":
        from .buyer_mcp_server import main as guardrail_main

        guardrail_main(args)
        return 0
    if command in ("--version", "-V"):
        from .buyer_init import package_version

        print(package_version())
        return 0

    print("usage:\n" + USAGE, file=sys.stderr if command not in ("-h", "--help") else sys.stdout)
    return 0 if command in ("-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main())
