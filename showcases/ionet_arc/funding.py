"""Validate an io.net payment request and describe an Arc-funded route.

This module never signs, bridges, transfers, or marks a request as paid.
"""
import re
import time
from decimal import Decimal, InvalidOperation

from .adapter import ENDPOINT, PilotError, digest
from .arc import NETWORKS, USDC

SOLANA = {
    "mainnet": ("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    "testnet": ("solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"),
}


def public_key(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise PilotError("Expected a Solana public key")
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    try:
        for char in value:
            n = n * 58 + alphabet.index(char)
    except ValueError:
        raise PilotError("Invalid base58 public key") from None
    size = len(value) - len(value.lstrip("1")) + (n.bit_length() + 7) // 8
    if size != 32 or n == 0:
        raise PilotError("Expected a nonzero 32-byte public key")
    return value


def payment_plan(state, network, operational_wallet, max_payment_usdc, now=None):
    now = time.time() if now is None else now
    if state.get("endpoint") != ENDPOINT or state.get("phase") != "payment_required":
        raise PilotError("Need a payment_required state captured from io.net")
    if not 0 <= now - state.get("created_at", 0) <= 300:
        raise PilotError("Payment request is stale; obtain a fresh provider request")
    payment = state.get("payment")
    if not isinstance(payment, dict) or payment.get("x402Version") != 2:
        raise PilotError("Expected an x402 v2 payment envelope")
    accepts = payment.get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        raise PilotError("Payment envelope has no first accepted option")
    quote = accepts[0]
    expected_network, mint = SOLANA[network]
    if quote.get("scheme") != "exact" or quote.get("network") != expected_network or quote.get("asset") != mint:
        raise PilotError("Payment scheme, network, or USDC mint does not match the Arc funding environment")
    amount = quote.get("amount", quote.get("maxAmountRequired"))
    if "amount" in quote and "maxAmountRequired" in quote and quote["amount"] != quote["maxAmountRequired"]:
        raise PilotError("Conflicting payment amounts")
    if not isinstance(amount, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", amount):
        raise PilotError("Expected a positive integer atomic-unit payment amount")
    usd = Decimal(amount) / Decimal(1_000_000)
    try:
        limit = Decimal(str(max_payment_usdc))
        if not limit.is_finite() or not 0 < usd <= limit:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise PilotError("Payment exceeds the configured USDC limit") from None
    recipient = public_key(quote.get("payTo"))
    wallet = public_key(operational_wallet)
    if wallet == recipient:
        raise PilotError("Operational wallet must be distinct from the provider payment address")
    return {
        "version": 1, "executable": False, "payment_sha256": digest(payment),
        "source": {"chain_id": NETWORKS[network]["chain_id"], "cctp_domain": 26, "usdc": USDC},
        "bridge": {"protocol": "CCTP", "destination_domain": 5,
                   "recipient_wallet": wallet, "minimum_net_usdc": format(usd, "f"),
                   "gross_amount": None, "requires_fee_quote": True},
        "provider_payment": {"network": expected_network, "asset": mint, "payTo": recipient,
                             "amount": amount, "decimals": 6, "single_transfer": True},
        "remaining": ["Quote bridge fees and reserve Arc gas",
                      "Bridge to the operational wallet; persist burn, attestation and mint progress",
                      "Confirm actual destination USDC balance and arrange Solana transaction fees",
                      "Revalidate the current io.net request, pay its exact amount once, record the transaction",
                      "Confirm io.net credited the account, then reconcile before retrying deployment"],
    }
