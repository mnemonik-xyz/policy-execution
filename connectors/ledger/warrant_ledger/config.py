"""TOML configuration. Secrets never live here; adapters read them from the environment."""
import tomllib
from dataclasses import dataclass, field


@dataclass
class Chain:
    rpc_url: str
    chain_id: int
    escrow: str
    customer: str
    from_block: int = 0
    confirmations: int = 12
    max_range: int = 2000


@dataclass
class Beancount:
    path: str
    escrow_account: str = "Assets:Escrow:Warrant"
    wallet_account: str = "Assets:Buyer:Wallet"
    expense_prefix: str = "Expenses:Warrant"
    unpaid_after_days: int = 30


@dataclass
class Config:
    chain: Chain
    store: str
    warrant_ids: str = "warrant-ids"
    beancount: Beancount | None = None
    vendors: dict = field(default_factory=dict)


def load(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    chain = Chain(**raw["chain"])
    chain.escrow = chain.escrow.lower()
    chain.customer = chain.customer.lower()
    bean = Beancount(**raw["beancount"]) if "beancount" in raw else None
    vendors = {k.lower(): v for k, v in raw.get("vendors", {}).items()}
    return Config(
        chain=chain,
        store=raw["store"]["path"],
        warrant_ids=raw.get("tools", {}).get("warrant_ids", "warrant-ids"),
        beancount=bean,
        vendors=vendors,
    )
