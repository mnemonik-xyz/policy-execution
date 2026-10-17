# warrant-buyer

Buyer guardrail for [Warrant](https://github.com/mnemonik-xyz/warrant). An AI agent buys
compute with USDC on Arc. The agent holds no key that can pay: this local guardrail holds the
wallet key and the spending cap, funds the escrow and signs the acceptance.

```sh
uvx warrant-buyer init      # makes a new wallet and prints the MCP client configuration
uvx warrant-buyer status    # shows the wallet address and its USDC balance
```

Send about 1.10 USDC to the printed address on Arc mainnet. Add the printed configuration to
Claude Desktop, Cursor or Claude Code. Then ask the agent: "Transcribe audio.mp3."

The wallet is new and only for this guardrail. Send only what you are ready to spend. The keys
stay in `~/.warrant-buyer`, readable by your user only.

Platforms: macOS (Apple Silicon and Intel) and Linux (x86-64 and ARM64). Windows is not
supported yet.

The mainnet purchase path has not run with money yet. Read the limits in the
[showcase README](https://github.com/mnemonik-xyz/policy-execution/tree/main/showcases/ionet_arc).
