//! The obligatory safety checks S1 to S25 (spec section 7). The policy cannot
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

/// Asset metadata read from the chain, never from the counterparty (S9).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AssetFacts {
    pub decimals: u8,
    pub risk_flags: Vec<RiskFlag>,
    pub transfer_fee: Option<TransferFee>,
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
/// and every account and asset lives on its leg's chain.
pub fn s5_s6_terms(terms: &Terms, role: Role, own: &[AccountId]) -> Check {
    for leg in [&terms.leg_a, &terms.leg_b] {
        let on_chain = [&leg.sender, &leg.receiver, &leg.refund_to].iter().all(|a| a.chain() == leg.chain);
        ensure(on_chain && leg.asset.chain() == leg.chain, code::S8, "account or asset on another chain")?;
    }
    let own_leg = terms.leg(role.own_leg());
    let their_leg = terms.leg(role.counterparty_leg());
    ensure(own.contains(&own_leg.refund_to), code::S6, "own refund does not pay an own account")?;
    ensure(own.contains(&their_leg.receiver), code::S5, "counterparty claim does not pay an own account")
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
pub fn s7(leg: &Leg, policy: &CompiledPolicy, observed: Option<&ContractObservation>) -> Check {
    let pin = policy.pin_for(&leg.chain, &leg.lock.contract);
    let ok = match (pin, observed) {
        (Some(ContractPinSpec::BitcoinTemplate(t)), obs) => {
            t == crate::bitcoin::TEMPLATE_ID
                && leg.lock.contract == crate::bitcoin::TEMPLATE_ID
                && match obs {
                    // Before a lock exists, the template derivation itself is the identity.
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
            crate::solana::parse_key(&leg.lock.contract).is_some_and(|program| p.matches(&program, &leg.lock.swap_id, facts))
        }
        _ => false,
    };
    ensure(ok, code::S7, format!("{} on {} is not a pinned contract", leg.lock.contract, leg.chain))
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
        verified::s11_holds(ta, now_a, pa.clock, tb, now_b, pb.clock, pb.d_observe_secs, pa.d_confirm_secs, margin),
        code::S11,
        "timeout gap below D_observe(B) + D_confirm(A) + D_margin",
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
        .check_policy_version(policy.policy.version)
        .map_err(|e| Violation { code: code::S22, detail: format!("{e:?}") })
}

pub fn s23(policy: &CompiledPolicy, build: &Hash32) -> Check {
    ensure(&policy.policy.evaluator_id == build, code::S23, "evaluator build differs from the pinned build")
}

/// Which leg an observation key names.
pub fn leg_of(role: Role, own: bool) -> LegName {
    if own { role.own_leg() } else { role.counterparty_leg() }
}
