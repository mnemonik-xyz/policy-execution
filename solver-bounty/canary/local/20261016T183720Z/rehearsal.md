# Rehearsal on loopback Anvil

- Commit: fac8b909893cf168157d84e51ce103fdb0351299
- Image ID: 0x56d9ff10f85f9f023a32c632dcd11ae95a9cd7024d34e4f489c1b392a299bff2
- Instance hash: 0x48ed79b023d0ca12bcfea54afb9146e4262cb43c18df89fc77579f740118a6e0
- Build seconds (configure cargo build of this commit; else the image-ID check): 123
- Image-ID check seconds (warrant-host solver-image-id, warm cache): 1
- Prove seconds (whole prove step): 332
- Wrap seconds (docker stats samples): 19
- Peak RSS of prove (prove.time): 1460980 kB
- Wrap container peak memory: 7553773731 bytes
- Free disk on / after the run: 24.1 GB
- Refund: refund past settleBy restored the buyer balance; retry quote succeeded (refund run /opt/warrant-canary/src/solver-bounty/canary/local/20261016T184713Z, retry /opt/warrant-canary/src/solver-bounty/canary/local/20261016T184929Z)

Health delta against health-before.json:

    mcp: 200 -> 200 ok
    kaneo: 200 -> 200 ok
    vaultwarden: 200 -> 200 ok
    caddy: 308 -> 308 ok
    ollama: 200 -> 200 ok

realProof: true
