"""Copies confirmed InvoiceEscrow logs from the chain into the event store."""
from .chain import decode


class DeepReorg(RuntimeError):
    """A block the store already holds changed. The connector stops; it does not repair."""


def sync(rpc, store, chain):
    """Reads logs from the stored cursor up to head - confirmations. Returns the
    number of new logs. A second call with no new blocks is a no-op."""
    actual = int(rpc.call("eth_chainId"), 16)
    if actual != chain.chain_id:
        raise RuntimeError(f"RPC serves chain {actual}, configuration says {chain.chain_id}")
    row = store.cursor(chain.chain_id, chain.escrow)
    start, last_hash = (row["next_block"], row["last_hash"]) if row else (chain.from_block, None)
    if last_hash is not None and start > 0 and rpc.block(start - 1)["hash"] != last_hash:
        raise DeepReorg(f"Block {start - 1} changed after it was confirmed; rescan with --from")
    safe = rpc.block_number() - chain.confirmations
    if safe < start:
        return 0
    blocks, added = {}, 0
    for lo in range(start, safe + 1, chain.max_range):
        hi = min(lo + chain.max_range - 1, safe)
        for log in rpc.logs(chain.escrow, lo, hi):
            if log.get("removed"):
                continue
            decoded = decode(log)
            if decoded is None:
                continue
            number = int(log["blockNumber"], 16)
            if number not in blocks:
                blocks[number] = rpc.block(number)
            if blocks[number]["hash"] != log["blockHash"]:
                raise DeepReorg(f"Block {number} hash differs between the log and the block")
            added += store.add_log(chain.chain_id, log, blocks[number], *decoded)
    store.set_cursor(chain.chain_id, chain.escrow, safe + 1, rpc.block(safe)["hash"])
    store.commit()
    return added


def rescan(store, chain, from_block):
    """Manual recovery after a deep reorganization: drop logs from a block and
    move the cursor back. Bookings already written to a ledger stay; the
    exceptions report shows any that no longer have an event."""
    store.delete_from(chain.chain_id, from_block)
    store.set_cursor(chain.chain_id, chain.escrow, from_block, None)
    store.commit()
