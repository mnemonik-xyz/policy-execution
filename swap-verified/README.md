# Swap evaluator and timeout arithmetic (verified)

This crate holds the policy evaluator and the timeout arithmetic of
[Warrant for swaps](../../swap/spec.md) (step W1 of the
[implementation specification](../../swap/implementation.md)). The executable
code and its Verus specification share one source file, `src/lib.rs`. The
crate has no dependency on RISC Zero, on `k256` or on Mnemonik. It does not
touch the invoice `Rule`, so the invoice guest image id does not change.

## What is proved

| Item | Statement |
|---|---|
| `evaluate3` | Returns exactly `kleene(rule, facts3)`, the strong Kleene meaning, for every rule and facts |
| `decide` | `Allow` ⇔ `kleene = Some(true)`; `Deny` ⇔ `kleene = Some(false)`; otherwise `Ask` |
| `kleene_sound`, `decision_sound` | `Allow` holds, and `Deny` fails, for every way to fill in the unknown facts |
| `earliest_exec`, `latest_exec`, `timeout_gap`, `reveal_window`, `s11_holds`, `s12_holds` | Equal their integer specifications; no overflow |
| `earliest_is_conservative`, `latest_is_conservative` | In a chain model where the real time of the next `n` blocks and the clock drift stay inside the profile bounds, a refund is never valid before `earliest_real` and always valid from `latest_real` on |
| `s11_sound` | If S11 holds, then whenever leg A is refundable, leg B has been refundable for at least `d_refund + d_observe + d_confirm + d_margin` |
| `s12_sound` | If S12 holds, leg B is not refundable before `now + d_confirm + d_margin` |
| `model_is_satisfiable` | The chain model is not vacuous |

The clock model has no fixed block interval, because blocks arrive at random
(spec 7.3). A profile bounds the real time of the next `n` blocks from now at a
stated failure probability: at least `n * fast_block_secs - fast_slack_secs`
(and at least 0), at most `n * slow_block_secs + slow_slack_secs`. A time lock
also waits for `time_settle_blocks` new blocks after the chain clock passes `T`
(six on Bitcoin, where the median time past decides, BIP 113). The model holds
at the profile's failure probability; the proofs are exact inside it.

Not proved here: the fact builder, the obligatory checks S1 to S27, chain
profiles, transaction decoders and the clock bounds themselves. They live in
`swap-core` and are tested. The clock bounds are assumptions from measured data
(spec section 12).

## Reproduce

```sh
python3 swap-verified/verify.py --verus /path/to/verus --mutations
cargo test -p warrant-swap-verified
```

The runner pins Verus `0.2026.09.20.aef82ed`, uses `--no-cheating` and prints
the source SHA-256. With `--mutations` it applies 35 changes to executable code
only (for example "Ask becomes Allow", "unknown conjunct ignored", "latest uses
fast bound", "S11 drops refund time", "reveal window drops margin") and
requires a genuine verification failure for each.

## Results

2026-10-07, after the n-block clock model and `D_refund` (spec 7.3), Verus
`0.2026.09.20.aef82ed` built from source with Z3 4.16.0: **52 verified, 0
errors**; **35 of 35 mutations rejected**; 12 native tests passed.

2026-10-05, the first version: 49 verified, 0 errors; 29 of 29 mutations
rejected.
