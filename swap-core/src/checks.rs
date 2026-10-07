//! The obligatory safety checks S1 to S25 and S27 (spec section 7). The policy cannot
//! switch them off. Each returns `Ok(())` or a `Violation` with a fixed reason
//! code. On an entry action a violation denies; on an exit action it halts.

use crate::caip::{AccountId, AssetId, ChainId, Family};
use crate::dsl::{CompiledPolicy, ContractPinSpec};
use crate::evm::ContractFacts;
use crate::facts::TransferFee;
use crate::ledger::LedgerState;
use crate::profile::{ChainProfile, ProfileSet, RefundMethod, ValueBand};
use crate::solana::ProgramFacts;
use crate::types::{HashAlg, Leg, LegName, RiskFlag, Role, Terms, TimelockSpec};
use crate::verified::{self, ChainNow, Timelock};
use crate::Hash32;
use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Violation {
    pub code: &'static str,
    pub detail: String,
}

impl fmt::Display for Violation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

pub type Check = Result<(), Violation>;

pub fn violation(code: &'static str, detail: impl Into<String>) -> Violation {
    Violation { code, detail: detail.into() }
}

pub fn fail(code: &'static str, detail: impl Into<String>) -> Check {
    Err(violation(code, detail))
}

fn ensure(ok: bool, code: &'static str, detail: impl Into<String>) -> Check {
    if ok { Ok(()) } else { fail(code, detail) }
}

pub mod code {
    pub const S1: &str = "S1_HASH_ALG";
    pub const S2: &str = "S2_PREIMAGE_LEN";
    pub const S3: &str = "S3_HASHLOCK_MISMATCH";
    pub const S4: &str = "S4_HASHLOCK_REUSED";
    pub const S5: &str = "S5_RECEIVER";
    pub const S6: &str = "S6_REFUND_ACCOUNT";
    pub const S7: &str = "S7_CONTRACT_IDENTITY";
    pub const S8: &str = "S8_AMOUNT_OR_ASSET";
    pub const S8_FLAG: &str = "S8_FORBIDDEN_RISK_FLAG";
    pub const S9: &str = "S9_ASSET_SOURCE";
    pub const S10: &str = "S10_SWAP_ID";
    pub const S11: &str = "S11_TIMEOUT_GAP";
    pub const S12: &str = "S12_REVEAL_DEADLINE";
    pub const S13: &str = "S13_ENTRY_WINDOW";
    pub const S14: &str = "S14_FINALITY";
    pub const S15: &str = "S15_FEE_RESERVE";
    pub const S16: &str = "S16_WATCHER";
    pub const S17: &str = "S17_REFUND_PREPARED";
    pub const S19: &str = "S19_FEE_RAISE";
    pub const S21: &str = "S21_WARRANT_CONSUMED";
    pub const S22: &str = "S22_POLICY_ROLLBACK";
    pub const S23: &str = "S23_EVALUATOR";
    pub const S24: &str = "S24_TRANSACTION";
    pub const CHAIN: &str = "CHAIN_UNSUPPORTED";
    pub const ROLE: &str = "ACTION_NOT_FOR_ROLE";
    pub const TIMELOCK: &str = "TIMELOCK_FORM";
    pub const BAND: &str = "VALUE_BAND_EXCEEDED";
    pub const ACCEPT: &str = "ACCEPT_BINDING";
    pub const PRICE: &str = "PRICE_UNKNOWN";
    pub const S27: &str = "S27_RECEIVER";
}

/// A lock as read from its chain by the profile's observation adapter. On
/// Bitcoin the adapter reports the output; S7 re-derivation of the output script
/// from the terms proves `H`, the keys and `T`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LockFacts {
    pub contract: String,
    pub swap_id: Hash32,
    pub hash_alg: HashAlg,
    pub hashlock: Hash32,
    /// The lock contract or script enforces `len(s) = 32`.
    pub preimage_len_enforced: bool,
    /// The first height or chain time at which the refund is valid, as in
    /// `Leg::refund_valid_from` (on Bitcoin: the CLTV operand plus one).
    pub timelock: Timelock,
    pub receiver: AccountId,
    pub refund_to: AccountId,
    pub asset: AssetId,
    /// Amount held by the lock, net of any transfer fee.
    pub net_amount: u128,
    pub confirmations: u64,
    pub finalized: Option<bool>,
    /// Bitcoin: the HTLC output and its script.
    pub outpoint: Option<(Hash32, u32)>,
    pub script_pubkey: Option<Vec<u8>>,
}

/// An own payee of a leg: the receiver of a claim or the `refund_to` of a refund.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Payee {
    Receiver,
    RefundTo,
}

/// Whether an own payee can receive the leg asset now (S27), read from the chain by
/// the profile's reader. Each variant names the accounts it was read for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReceiverFacts {
    /// An EVM token: its blocklist state (for example USDC `isBlacklisted`) for the
    /// payee and for the HTLC that pays it, and its pause flag.
    Evm { token: [u8; 20], payee: [u8; 20], htlc: [u8; 20], payee_blocked: bool, htlc_blocked: bool, paused: bool },
    /// A Solana token account of the payee.
    Solana {
        account: Hash32,
        initialized: bool,
        frozen: bool,
        /// Token-2022 `MemoTransfer` with required incoming memos.
        memo_required: bool,
        mint: Hash32,
        owner: Hash32,
        program: Hash32,
        /// Token-2022 `Pausable`: the leg mint is paused now. False for a mint
        /// without the extension.
        mint_paused: bool,
        /// The token account of the escrow that pays this payee, and whether it is
        /// frozen. Read once that lock exists.
        escrow: Option<(Hash32, bool)>,
    },
}

/// Asset metadata read from the chain, never from the counterparty (S9).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AssetFacts {
    pub decimals: u8,
    pub risk_flags: Vec<RiskFlag>,
    pub transfer_fee: Option<TransferFee>,
    /// Solana: the program that owns the mint. `None` on other chains.
    pub token_program: Option<Hash32>,
}

impl AssetFacts {
    pub fn risk_mask(&self) -> u32 {
        RiskFlag::mask(&self.risk_flags)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ContractObservation {
    Bitcoin { script_pubkey: Vec<u8> },
    Evm(ContractFacts),
    Solana(ProgramFacts),
}

/// The signer runtime's state that S16 and S17 read.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Runtime {
    pub watchers_armed: bool,
    pub prepared_refund: bool,
    pub solana_mode: crate::solana::SolanaMode,
}

pub fn profile_for<'a>(chain: &ChainId, policy: &CompiledPolicy, profiles: &'a ProfileSet) -> Result<&'a ChainProfile, Violation> {
    let profile = profiles.get(chain).filter(|p| p.missing_obligatory().is_empty());
    match (profile, policy.policy.chains.get(chain)) {
        (Some(p), Some(entry)) if p.hash() == entry.profile_hash => Ok(p),
        _ => Err(Violation { code: code::CHAIN, detail: format!("{chain} has no pinned, complete profile") }),
    }
}

/// S1: both legs use SHA-256.
pub fn s1(terms: &Terms) -> Check {
    ensure(
        terms.leg_a.lock.hash_alg == HashAlg::Sha256 && terms.leg_b.lock.hash_alg == HashAlg::Sha256,
        code::S1,
        "both legs must use sha256",
    )
}

/// S2: both locks enforce a 32-byte preimage.
pub fn s2(terms: &Terms, pa: &ChainProfile, pb: &ChainProfile) -> Check {
    ensure(
        terms.leg_a.lock.preimage_len == 32
            && terms.leg_b.lock.preimage_len == 32
            && pa.template_enforces_len32
            && pb.template_enforces_len32,
        code::S2,
        "both locks must enforce a 32-byte preimage",
    )
}

/// S3: both legs carry the same `H`.
pub fn s3(terms: &Terms) -> Check {
    ensure(terms.leg_a.lock.hashlock == terms.leg_b.lock.hashlock, code::S3, "legs carry different hashlocks")
}

/// S4: `H` was never used before (checked at accept).
pub fn s4(terms: &Terms, ledger: &LedgerState) -> Check {
    ensure(!ledger.ledger.consumed_hashlocks.contains(&terms.leg_a.lock.hashlock), code::S4, "hashlock already used")
}

/// S5 and S6 on the terms: what this party receives goes to its own accounts,
/// and every account and asset lives on its leg's chain. On Bitcoin the leaf keys
/// decide who can spend: the claim key of the counterparty leg and the refund key
/// of the own leg must be own keys.
pub fn s5_s6_terms(terms: &Terms, role: Role, own: &[AccountId], own_bitcoin_keys: &[Hash32]) -> Check {
    for leg in [&terms.leg_a, &terms.leg_b] {
        let on_chain = [&leg.sender, &leg.receiver, &leg.refund_to].iter().all(|a| a.chain() == leg.chain);
        ensure(on_chain && leg.asset.chain() == leg.chain, code::S8, "account or asset on another chain")?;
    }
    let own_leg = terms.leg(role.own_leg());
    let their_leg = terms.leg(role.counterparty_leg());
    ensure(own.contains(&own_leg.refund_to), code::S6, "own refund does not pay an own account")?;
    ensure(own.contains(&their_leg.receiver), code::S5, "counterparty claim does not pay an own account")?;
    let bitcoin = |leg: &Leg| leg.chain.family() == Some(Family::Bitcoin);
    if bitcoin(own_leg) {
        let refund = own_leg.lock.keys.map(|k| k.refund);
        ensure(refund.is_some_and(|k| own_bitcoin_keys.contains(&k)), code::S6, "own leg refund key is not an own key")?;
    }
    if bitcoin(their_leg) {
        let claim = their_leg.lock.keys.map(|k| k.receiver);
        ensure(claim.is_some_and(|k| own_bitcoin_keys.contains(&k)), code::S5, "counterparty leg claim key is not an own key")?;
    }
    Ok(())
}

/// S5, S6, S8, S9 and S10 on an observed lock: it is the agreed lock.
pub fn observed_lock(leg: &Leg, facts: &LockFacts, expected_timelock: Option<Timelock>) -> Check {
    ensure(facts.receiver == leg.receiver, code::S5, "observed receiver differs from the terms")?;
    ensure(facts.refund_to == leg.refund_to, code::S6, "observed refund account differs from the terms")?;
    ensure(facts.asset == leg.asset, code::S9, "observed asset differs from the terms")?;
    ensure(facts.net_amount == leg.amount, code::S8, "observed net amount differs from the terms")?;
    ensure(
        facts.hash_alg == HashAlg::Sha256 && facts.preimage_len_enforced,
        code::S2,
        "observed lock does not enforce a 32-byte sha256 preimage",
    )?;
    ensure(facts.hashlock == leg.lock.hashlock, code::S3, "observed hashlock differs")?;
    ensure(facts.swap_id == leg.lock.swap_id, code::S10, "observed lock binds another swap id")?;
    if let Some(t) = expected_timelock {
        ensure(facts.timelock == t, code::S11, "observed timelock differs from the terms")?;
    }
    Ok(())
}

/// S7: the lock contract is a pinned contract, proved by its chain identity.
/// `locked`: the lock of this leg exists now. It is false only for the own lock
/// before it is made; on Solana the escrow address then holds no account, or only
/// lamports (`ProgramFacts::escrow_ready`).
pub fn s7(leg: &Leg, policy: &CompiledPolicy, observed: Option<&ContractObservation>, locked: bool) -> Check {
    let pin = policy.pin_for(&leg.chain, &leg.lock.contract);
    let ok = match (pin, observed) {
        (Some(ContractPinSpec::BitcoinTemplate(t)), obs) => {
            t == crate::bitcoin::TEMPLATE_ID
                && leg.lock.contract == crate::bitcoin::TEMPLATE_ID
                && match obs {
                    // Before a lock exists (accept, own lock), the template derivation is
                    // the identity. An observed lock always passes its output script.
                    None => crate::bitcoin::htlc_script_pubkey(&leg.lock).is_ok(),
                    Some(ContractObservation::Bitcoin { script_pubkey }) => {
                        crate::bitcoin::htlc_script_pubkey(&leg.lock).as_ref() == Ok(script_pubkey)
                    }
                    Some(_) => false,
                }
        }
        (Some(ContractPinSpec::Evm(p)), Some(ContractObservation::Evm(facts))) => {
            crate::from_hex_array::<20>(&leg.lock.contract).is_some_and(|a| p.matches(&a, facts))
        }
        (Some(ContractPinSpec::Solana(p)), Some(ContractObservation::Solana(facts))) => {
            crate::solana::parse_key(&leg.lock.contract).is_some_and(|program| p.matches(&program, &leg.lock.swap_id, facts, locked))
        }
        _ => false,
    };
    let escrow_state = match observed {
        Some(ContractObservation::Solana(f)) => {
            crate::solana::parse_key(&leg.lock.contract).is_some_and(|program| !f.escrow_ready(&program, locked))
        }
        _ => false,
    };
    if escrow_state {
        return fail(code::S7, "the escrow account is not in the state that the action needs");
    }
    ensure(ok, code::S7, format!("{} on {} is not a pinned contract", leg.lock.contract, leg.chain))
}

/// S27: the own `payee` of `leg` can receive the leg asset now. A Bitcoin output and
/// a native coin need no facts. An EVM token must block neither the payee nor the
/// HTLC and must not be paused; the facts name the leg's ERC-20 contract, the payee
/// and the leg's HTLC. A Solana token account must be the payee's associated account
/// for the leg mint under the mint's token program (`token_program`, read from the
/// chain; SPL Token or Token-2022), initialized, not frozen and without required
/// incoming memos, and the mint must not be paused. When the lock that pays the payee
/// already exists (`locked`), its escrow token account must not be frozen either.
/// Unknown facts fail: the account must already exist and be able to receive.
pub fn s27(leg: &Leg, payee: Payee, facts: Option<&ReceiverFacts>, token_program: Option<Hash32>, locked: bool) -> Check {
    let account = match payee {
        Payee::Receiver => &leg.receiver,
        Payee::RefundTo => &leg.refund_to,
    };
    let family = leg.chain.family();
    if family == Some(Family::Bitcoin) || leg.asset.is_native() {
        return Ok(());
    }
    let ok = match (family, facts) {
        (Some(Family::Evm), Some(ReceiverFacts::Evm { token, payee: p, htlc, payee_blocked, htlc_blocked, paused })) => {
            leg.asset.erc20_address() == Some(*token)
                && account.evm_address() == Some(*p)
                && crate::from_hex_array::<20>(&leg.lock.contract) == Some(*htlc)
                && !payee_blocked
                && !htlc_blocked
                && !paused
        }
        (
            Some(Family::Solana),
            Some(ReceiverFacts::Solana { account: a, initialized, frozen, memo_required, mint, owner, program, mint_paused, escrow }),
        ) => {
            let token = leg.asset.spl_mint().zip(token_program.filter(crate::solana::is_token_program));
            let token = token.map(|(m, tp)| crate::solana::TokenAccounts { mint: m, token_program: tp });
            let expected = token.zip(account.solana_key()).and_then(|(t, o)| {
                crate::solana::associated_token_address(&o, &t).map(|ata| (ata, t.mint, o, t.token_program))
            });
            // The escrow token account of an existing lock: the payee is paid from it.
            let escrow_ok = !locked
                || token
                    .zip(crate::solana::parse_key(&leg.lock.contract))
                    .and_then(|(t, program)| {
                        crate::solana::escrow_address(&program, &leg.lock.swap_id)
                            .and_then(|e| crate::solana::associated_token_address(&e, &t))
                    })
                    .is_some_and(|expected_escrow| *escrow == Some((expected_escrow, false)));
            expected == Some((*a, *mint, *owner, *program)) && *initialized && !frozen && !memo_required && !mint_paused && escrow_ok
        }
        _ => false,
    };
    ensure(ok, code::S27, format!("own {payee:?} on {} cannot receive the leg asset now", leg.chain))
}

/// S8: flags that always deny.
pub fn s8_flags(assets: &[&AssetFacts]) -> Check {
    for a in assets {
        if let Some(f) = a.risk_flags.iter().find(|f| f.always_denied()) {
            return fail(code::S8_FLAG, f.name());
        }
    }
    Ok(())
}

/// S10: the locks bind this swap id, which no earlier swap consumed (at accept).
pub fn s10(terms: &Terms, ledger: &LedgerState, at_accept: bool) -> Check {
    ensure(
        terms.leg_a.lock.swap_id == terms.swap_id && terms.leg_b.lock.swap_id == terms.swap_id,
        code::S10,
        "a lock binds another swap id",
    )?;
    ensure(!(at_accept && ledger.ledger.consumed_swap_ids.contains(&terms.swap_id)), code::S10, "swap id already consumed")
}

/// The timelock form that the chain's lock accepts.
pub fn timelock_form(leg: &Leg) -> Check {
    let ok = match leg.chain.family() {
        Some(Family::Bitcoin) => crate::bitcoin::refund_leaf(&leg.lock.timelock, &[0; 32]).is_ok(),
        Some(Family::Evm) | Some(Family::Solana) => matches!(leg.lock.timelock, TimelockSpec::Time(_)),
        None => false,
    };
    ensure(ok, code::TIMELOCK, format!("timelock form not supported on {}", leg.chain))
}

/// S11 through the verified arithmetic.
pub fn s11(
    ta: Timelock, now_a: ChainNow, pa: &ChainProfile,
    tb: Timelock, now_b: ChainNow, pb: &ChainProfile,
    margin: u64,
) -> Check {
    ensure(
        verified::s11_holds(ta, now_a, pa.clock, tb, now_b, pb.clock, pb.d_refund(), pb.d_observe_secs, pa.d_confirm_secs, margin),
        code::S11,
        "timeout gap below D_refund(B) + D_observe(B) + D_confirm(A) + D_margin",
    )
}

/// S12 through the verified arithmetic.
pub fn s12(tb: Timelock, now_b: ChainNow, pb: &ChainProfile, margin: u64) -> Check {
    ensure(verified::s12_holds(tb, now_b, pb.clock, pb.d_confirm_secs, margin), code::S12, "past the reveal deadline")
}

/// S14: the counterparty lock has the band's finality and evidence.
pub fn s14(facts: &LockFacts, method: crate::facts::EvidenceMethod, band: &ValueBand) -> Check {
    let final_ok = if band.finalized_tag { facts.finalized == Some(true) } else { facts.confirmations >= band.confirmations };
    ensure(final_ok, code::S14, "counterparty lock not final for the value band")?;
    ensure(method >= band.min_evidence, code::S14, "counterparty lock observed with weaker evidence than the band needs")
}

/// The value band for a notional; an unknown notional takes the strictest band.
pub fn band(profile: &ChainProfile, notional: Option<u64>) -> Result<&ValueBand, Violation> {
    match notional {
        Some(n) => profile.band(n).ok_or_else(|| Violation { code: code::BAND, detail: format!("notional {n} above every band") }),
        None => profile.value_bands.last().ok_or_else(|| Violation { code: code::BAND, detail: "no band".into() }),
    }
}

/// S15: native fee reserves cover a claim on the counterparty chain and a refund
/// on the own chain at the worst-case fee.
pub fn s15(
    reserves: &std::collections::BTreeMap<ChainId, u128>,
    own: &ChainProfile,
    their: &ChainProfile,
) -> Check {
    let have = |c: &ChainId| reserves.get(c).copied().unwrap_or(0);
    if own.chain == their.chain {
        let need = own.fees.worst_refund.saturating_add(their.fees.worst_claim);
        return ensure(have(&own.chain) >= need, code::S15, format!("fee reserve on {} below {need}", own.chain));
    }
    ensure(have(&own.chain) >= own.fees.worst_refund, code::S15, format!("refund fee reserve on {}", own.chain))?;
    ensure(have(&their.chain) >= their.fees.worst_claim, code::S15, format!("claim fee reserve on {}", their.chain))
}

pub fn s16(runtime: &Runtime) -> Check {
    ensure(runtime.watchers_armed, code::S16, "watchers are not armed for this swap")
}

pub fn s17(profile: &ChainProfile, runtime: &Runtime) -> Check {
    ensure(
        profile.refund == RefundMethod::Permissionless || runtime.prepared_refund,
        code::S17,
        "no prepared refund and the refund is not permissionless",
    )
}

pub fn s19(profile: &ChainProfile) -> Check {
    ensure(profile.fees.raise != crate::profile::FeeRaise::None, code::S19, "no fee-raising method")
}

/// S21: a warrant is used once.
pub fn s21(ledger: &LedgerState, warrant_hash: &Hash32) -> Check {
    ensure(!ledger.ledger.consumed_warrants.contains(warrant_hash), code::S21, "warrant already used")
}

pub fn s22(ledger: &LedgerState, policy: &CompiledPolicy) -> Check {
    ledger
        .check_policy(policy.policy.version, &policy.policy_hash)
        .map_err(|e| Violation { code: code::S22, detail: format!("{e:?}") })
}

pub fn s23(policy: &CompiledPolicy, build: &Hash32) -> Check {
    ensure(&policy.policy.evaluator_id == build, code::S23, "evaluator build differs from the pinned build")
}

/// Which leg an observation key names.
pub fn leg_of(role: Role, own: bool) -> LegName {
    if own { role.own_leg() } else { role.counterparty_leg() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::profile::reference;
    use crate::types::Leg;

    /// Review finding (PR #7): a Bitcoin CLTV refund with operand `h` becomes valid
    /// only in block `h + 1`. S11 must use that block, or it underestimates the
    /// latest refund of a Bitcoin leg B by one worst-case block interval.
    #[test]
    fn s11_counts_the_extra_cltv_block_on_a_bitcoin_leg_b() {
        let btc = reference::bitcoin(reference::BITCOIN_MAINNET);
        let eth = reference::ethereum();
        let mut leg_b: Leg = serde_json::from_value(serde_json::json!({
            "chain": reference::BITCOIN_MAINNET,
            "asset": "bip122:000000000019d6689c085ae165831e93/slip44:0",
            "amount": "1",
            "sender": "bip122:000000000019d6689c085ae165831e93:bc1pa",
            "receiver": "bip122:000000000019d6689c085ae165831e93:bc1pb",
            "refund_to": "bip122:000000000019d6689c085ae165831e93:bc1pa",
            "lock": { "contract": crate::bitcoin::TEMPLATE_ID, "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                      "preimage_len": 32, "timelock": {"kind": "height", "value": 900_010}, "swap_id": crate::to_hex(&[2; 32]) }
        }))
        .unwrap();
        let now = 1_800_000_000;
        let now_b = ChainNow { tip_height: 900_000, now_real: now };
        let now_a = ChainNow { tip_height: 0, now_real: now };
        let margin = 600;
        // Bitcoin claims stay valid after T, so D_refund(B) counts.
        let need = btc.d_refund() + btc.d_observe_secs + eth.d_confirm_secs + margin;
        // T_A exactly enough if the refund of B were valid at block 900,010.
        let latest_10 = 10 * btc.clock.slow_block_secs + btc.clock.slow_slack_secs;
        let ta = Timelock::Time(now + latest_10 + need + eth.clock.max_lead_secs);
        assert!(s11(ta, now_a, &eth, Timelock::Height(900_010), now_b, &btc, margin).is_ok(), "raw operand passes");
        let tb = leg_b.refund_valid_from().unwrap();
        assert_eq!(tb, Timelock::Height(900_011));
        assert!(s11(ta, now_a, &eth, tb, now_b, &btc, margin).is_err(), "the true first valid block fails");
        // Times: nLockTime t is final once the median time past exceeds t.
        leg_b.lock.timelock = crate::types::TimelockSpec::Time(1_900_000_000);
        assert_eq!(leg_b.refund_valid_from(), Some(Timelock::Time(1_900_000_001)));
        // CSV counts confirmations: no adjustment once confirmed, unknown before.
        leg_b.lock.timelock = crate::types::TimelockSpec::RelativeBlocks(144);
        assert_eq!(leg_b.refund_valid_from(), None);
    }

    /// S27 on Solana: the payee's associated token account for the leg mint exists,
    /// is initialized, is not frozen and does not require incoming memos.
    #[test]
    fn s27_solana_token_account() {
        let owner_key = [4u8; 32];
        let mint_b58 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
        let chain = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
        let owner = bs58::encode(owner_key).into_string();
        let leg: Leg = serde_json::from_value(serde_json::json!({
            "chain": chain,
            "asset": format!("{chain}/token:{mint_b58}"),
            "amount": "1",
            "sender": format!("{chain}:{owner}"),
            "receiver": format!("{chain}:{owner}"),
            "refund_to": format!("{chain}:{owner}"),
            "lock": { "contract": bs58::encode([2u8; 32]).into_string(), "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                      "preimage_len": 32, "timelock": {"kind": "time", "value": 1_900_000_000}, "swap_id": crate::to_hex(&[2; 32]) }
        }))
        .unwrap();
        let program = crate::solana::key(crate::solana::TOKEN_2022_PROGRAM);
        let mint = crate::solana::parse_key(mint_b58).unwrap();
        let ata = crate::solana::associated_token_address(&owner_key, &crate::solana::TokenAccounts { mint, token_program: program }).unwrap();
        let good = ReceiverFacts::Solana {
            account: ata,
            initialized: true,
            frozen: false,
            memo_required: false,
            mint,
            owner: owner_key,
            program,
            mint_paused: false,
            escrow: None,
        };
        assert!(s27(&leg, Payee::Receiver, Some(&good), Some(program), false).is_ok());
        assert!(s27(&leg, Payee::RefundTo, Some(&good), Some(program), false).is_ok());
        let with = |edit: &dyn Fn(&mut ReceiverFacts)| {
            let mut f = good.clone();
            edit(&mut f);
            f
        };
        let bad = [
            with(&|f| if let ReceiverFacts::Solana { frozen, .. } = f { *frozen = true }),
            with(&|f| if let ReceiverFacts::Solana { memo_required, .. } = f { *memo_required = true }),
            with(&|f| if let ReceiverFacts::Solana { initialized, .. } = f { *initialized = false }),
            with(&|f| if let ReceiverFacts::Solana { account, .. } = f { *account = [9; 32] }),
            with(&|f| if let ReceiverFacts::Solana { mint, .. } = f { *mint = [9; 32] }),
            with(&|f| if let ReceiverFacts::Solana { owner, .. } = f { *owner = [9; 32] }),
            with(&|f| if let ReceiverFacts::Solana { program, .. } = f { *program = crate::solana::key(crate::solana::TOKEN_PROGRAM) }),
            with(&|f| if let ReceiverFacts::Solana { mint_paused, .. } = f { *mint_paused = true }),
        ];
        for (i, f) in bad.iter().enumerate() {
            assert!(s27(&leg, Payee::Receiver, Some(f), Some(program), false).is_err(), "case {i}");
        }
        // Once the paying lock exists, its escrow token account must not be frozen.
        let token = crate::solana::TokenAccounts { mint, token_program: program };
        let escrow = crate::solana::escrow_address(&[2u8; 32], &[2u8; 32]).unwrap();
        let escrow_ata = crate::solana::associated_token_address(&escrow, &token).unwrap();
        let with_escrow = |e: Option<(Hash32, bool)>| with(&|f| if let ReceiverFacts::Solana { escrow, .. } = f { *escrow = e });
        assert!(s27(&leg, Payee::Receiver, Some(&with_escrow(Some((escrow_ata, false)))), Some(program), true).is_ok());
        for e in [None, Some((escrow_ata, true)), Some(([9; 32], false))] {
            assert!(s27(&leg, Payee::Receiver, Some(&with_escrow(e)), Some(program), true).is_err(), "escrow {e:?}");
        }
        // Unknown facts, an unknown token program, a token program other than SPL
        // Token or Token-2022, or facts of another family fail.
        assert!(s27(&leg, Payee::Receiver, None, Some(program), false).is_err());
        assert!(s27(&leg, Payee::Receiver, Some(&good), None, false).is_err());
        let other = [0x0e; 32];
        let other_ata = crate::solana::associated_token_address(&owner_key, &crate::solana::TokenAccounts { mint, token_program: other }).unwrap();
        let foreign = with(&|f| {
            if let ReceiverFacts::Solana { account, program, .. } = f {
                *account = other_ata;
                *program = other;
            }
        });
        assert!(s27(&leg, Payee::Receiver, Some(&foreign), Some(other), false).is_err());
        let evm = ReceiverFacts::Evm { token: [0; 20], payee: [0; 20], htlc: [0; 20], payee_blocked: false, htlc_blocked: false, paused: false };
        assert!(s27(&leg, Payee::Receiver, Some(&evm), Some(program), false).is_err());
        // A native coin needs no facts.
        let mut native = leg.clone();
        native.asset = crate::caip::AssetId::parse(&format!("{chain}/slip44:501")).unwrap();
        assert!(s27(&native, Payee::Receiver, None, None, true).is_ok());
    }

    /// S7 on Solana (spec 8.4, D3): a lock that exists needs the program-owned escrow
    /// with the reference discriminator; before the own lock, no escrow account.
    #[test]
    fn s7_solana_escrow_state() {
        use crate::solana::{escrow_address, EscrowAccount, ProgramFacts, ESCROW_DISCRIMINATOR};
        let chain = reference::SOLANA_MAINNET;
        let (program, swap_id) = ([2u8; 32], [2u8; 32]);
        let program_b58 = bs58::encode(program).into_string();
        let owner = bs58::encode([4u8; 32]).into_string();
        let leg: Leg = serde_json::from_value(serde_json::json!({
            "chain": chain,
            "asset": format!("{chain}/slip44:501"),
            "amount": "1",
            "sender": format!("{chain}:{owner}"),
            "receiver": format!("{chain}:{owner}"),
            "refund_to": format!("{chain}:{owner}"),
            "lock": { "contract": program_b58, "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                      "preimage_len": 32, "timelock": {"kind": "time", "value": 1_900_000_000}, "swap_id": crate::to_hex(&swap_id) }
        }))
        .unwrap();
        let mut doc = crate::dsl::tests::policy_json(crate::dsl::tests::example_rule());
        doc["chains"][chain] = serde_json::json!({
            "profile_hash": crate::to_hex(&reference::solana().hash()),
            "contracts": [{"solana": {"program": program_b58}}]
        });
        let policy = crate::dsl::validate_policy(&doc.to_string(), &crate::dsl::tests::profiles()).unwrap();
        let escrow = EscrowAccount { owner: program, data_len: 113, discriminator: Some(ESCROW_DISCRIMINATOR) };
        let facts = |e: Option<EscrowAccount>| {
            ContractObservation::Solana(ProgramFacts {
                executable: true,
                upgrade_authority: None,
                escrow_address: escrow_address(&program, &swap_id).unwrap(),
                escrow: e,
            })
        };
        let other = EscrowAccount { discriminator: Some([0; 8]), ..escrow.clone() };
        let short = EscrowAccount { data_len: 7, discriminator: None, ..escrow.clone() };
        // An observed lock (responder's S13, reveal, claim).
        assert!(s7(&leg, &policy, Some(&facts(Some(escrow.clone()))), true).is_ok());
        for e in [Some(other), Some(short), None] {
            let v = s7(&leg, &policy, Some(&facts(e.clone())), true).unwrap_err();
            assert_eq!(v.code, code::S7, "{e:?}");
        }
        // The own lock, before the escrow exists. Lamports at the address do not block it.
        assert!(s7(&leg, &policy, Some(&facts(None)), false).is_ok());
        let funded = EscrowAccount { owner: [0; 32], data_len: 0, discriminator: None };
        assert!(s7(&leg, &policy, Some(&facts(Some(funded.clone()))), false).is_ok());
        assert_eq!(s7(&leg, &policy, Some(&facts(Some(funded))), true).unwrap_err().code, code::S7);
        assert_eq!(s7(&leg, &policy, Some(&facts(Some(escrow))), false).unwrap_err().code, code::S7);
        // Unknown contract facts fail closed.
        assert!(s7(&leg, &policy, None, false).is_err());
    }

    /// Spec 7.3: a Bitcoin claim stays valid after T_B until the refund is final,
    /// so S11 adds D_refund(B). A lock that rejects a late claim needs none.
    #[test]
    fn s11_counts_d_refund_while_a_claim_stays_valid() {
        let btc = reference::bitcoin(reference::BITCOIN_MAINNET);
        let eth = reference::ethereum();
        let now = 1_800_000_000;
        let margin = 600;
        let now_btc = ChainNow { tip_height: 900_000, now_real: now };
        let now_eth = ChainNow { tip_height: 0, now_real: now };
        // Leg B on Bitcoin, leg A on Ethereum: T_A covers all but D_refund(B).
        let tb = Timelock::Height(900_011);
        let latest_b = 11 * btc.clock.slow_block_secs + btc.clock.slow_slack_secs;
        let without_refund = btc.d_observe_secs + eth.d_confirm_secs + margin;
        let ta = |extra: u64| Timelock::Time(now + latest_b + without_refund + extra + eth.clock.max_lead_secs);
        assert!(s11(ta(0), now_eth, &eth, tb, now_btc, &btc, margin).is_err());
        assert!(s11(ta(btc.d_refund() - 1), now_eth, &eth, tb, now_btc, &btc, margin).is_err());
        assert!(s11(ta(btc.d_refund()), now_eth, &eth, tb, now_btc, &btc, margin).is_ok());
        // Leg B on Ethereum, whose reference claim closes at T_B: no D_refund.
        let tb = Timelock::Time(now + 4 * 3_600);
        let latest_b = 4 * 3_600 + eth.clock.max_lag_secs + eth.clock.slow_block_secs + eth.clock.slow_slack_secs;
        let need = eth.d_observe_secs + btc.d_confirm_secs + margin;
        // Leg A on Bitcoin: the n-block lower bound gives the earliest refund.
        let blocks = (latest_b + need + btc.clock.fast_slack_secs).div_ceil(btc.clock.fast_block_secs);
        let ta = Timelock::Height(900_000 + blocks);
        assert!(s11(ta, now_btc, &btc, tb, now_eth, &eth, margin).is_ok());
        assert!(s11(Timelock::Height(900_000 + blocks - 1), now_btc, &btc, tb, now_eth, &eth, margin).is_err());
    }
}
