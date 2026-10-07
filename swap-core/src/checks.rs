//! The obligatory safety checks S1 to S25 and S27 (spec section 7). The policy cannot
//! switch them off. Each returns `Ok(())` or a `Violation` with a fixed reason
//! code. On an entry action a violation denies; on an exit action it halts.

use crate::caip::{AccountId, AssetId, ChainId, Family};
use crate::dsl::{CompiledPolicy, ContractPinSpec};
use crate::evm::ContractFacts;
use crate::facts::TransferFee;
use crate::ledger::LedgerState;
use crate::profile::{ChainProfile, ProfileSet, RefundMethod, ValueBand};
use crate::solana::{EscrowAsset, ProgramFacts};
use crate::types::{HashAlg, Leg, LegName, RiskFlag, Role, Terms, TimelockSpec};
use crate::verified::{self, ChainNow, Timelock};
use crate::Hash32;
use std::fmt;

/// A failed check. `code`, and `cause` when a check wraps another one, are the
/// fixed reason codes that a decision record carries. `detail` is a local
/// diagnostic only: it can contain text from the proposed terms, so it never goes
/// into `reasons` (spec 4.1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Violation {
    pub code: &'static str,
    /// The code of the inner check that failed, when `code` wraps it (S13).
    pub cause: Option<&'static str>,
    pub detail: String,
}

impl fmt::Display for Violation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.cause {
            Some(cause) => write!(f, "{} ({cause}): {}", self.code, self.detail),
            None => write!(f, "{}: {}", self.code, self.detail),
        }
    }
}

pub type Check = Result<(), Violation>;

pub fn violation(code: &'static str, detail: impl Into<String>) -> Violation {
    Violation { code, cause: None, detail: detail.into() }
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
    pub const TERMS: &str = "TERMS_ENCODING";
    pub const ALLOW: &str = "POLICY_ALLOW";
    pub const ASK: &str = "POLICY_ASK";
    pub const DENY: &str = "POLICY_DENY";
    pub const EXIT: &str = "EXIT_ACTION";
    pub const S10_LOCK: &str = "S10_LOCK_ID";

    /// Every fixed reason code. `reasons` holds values from this list only.
    pub const ALL: &[&str] = &[
        S1, S2, S3, S4, S5, S6, S7, S8, S8_FLAG, S9, S10, S11, S12, S13, S14, S15, S16, S17, S19, S21, S22, S23, S24, S27,
        CHAIN, ROLE, TIMELOCK, BAND, ACCEPT, PRICE, TERMS, ALLOW, ASK, DENY, EXIT, S10_LOCK,
    ];
}

/// A lock as read from its chain by the profile's observation adapter. On
/// Bitcoin the adapter reports the output; S7 re-derivation of the output script
/// from the terms proves `H`, the keys and `T`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LockFacts {
    pub contract: String,
    /// The key under which the adapter read the lock: the EVM storage key, the seed
    /// of the Solana escrow PDA. On Bitcoin the adapter reports the `lock_id` of the
    /// terms; S7 proves it, because the claim leaf of the output script commits to it.
    pub lock_id: Hash32,
    pub hash_alg: HashAlg,
    pub hashlock: Hash32,
    /// The lock contract or script enforces `len(s) = 32`.
    pub preimage_len_enforced: bool,
    /// The first height or chain time at which the refund is valid, as in
    /// `Leg::refund_valid_from` (on Bitcoin: the CLTV operand plus one). For a
    /// relative block count: the confirming block plus the count. `swap-core`
    /// computes this value from the terms and the observation and denies another.
    pub timelock: Timelock,
    pub receiver: AccountId,
    pub refund_to: AccountId,
    pub asset: AssetId,
    /// Amount held by the lock, net of any transfer fee.
    pub net_amount: u128,
    /// Blocks from the block that contains the lock up to the block at which the
    /// provider read the lock (the report height), both included, as Bitcoin Core
    /// counts: 1 when the lock is in that block, 0 when it is unconfirmed.
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
        _ => Err(violation(code::CHAIN, format!("{chain} has no pinned, complete profile"))),
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
        let refund = own_leg.lock.refund_key;
        ensure(refund.is_some_and(|k| own_bitcoin_keys.contains(&k)), code::S6, "own leg refund_key is not an own key")?;
    }
    if bitcoin(their_leg) {
        let claim = their_leg.lock.claim_key;
        ensure(claim.is_some_and(|k| own_bitcoin_keys.contains(&k)), code::S5, "counterparty leg claim_key is not an own key")?;
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
    ensure(facts.lock_id == leg.lock.lock_id, code::S10_LOCK, "observed lock has another lock_id")?;
    if let Some(t) = expected_timelock {
        ensure(facts.timelock == t, code::S11, "observed timelock differs from the terms")?;
    }
    Ok(())
}

/// What the Solana escrow of `leg` holds (spec 8.4). `None` for a token whose mint,
/// or whose token program read from the chain (SPL Token or Token-2022), is unknown.
fn solana_escrow_asset(leg: &Leg, token_program: Option<Hash32>) -> Option<EscrowAsset> {
    if leg.asset.is_native() {
        return Some(EscrowAsset::Native);
    }
    let token_program = token_program.filter(crate::solana::is_token_program)?;
    leg.asset.spl_mint().map(|mint| EscrowAsset::Token(crate::solana::TokenAccounts { mint, token_program }))
}

/// S7: the lock contract is a pinned contract, proved by its chain identity.
/// `locked`: the lock of this leg exists now. It is false only for the own lock
/// before it is made; on Solana the escrow address then holds no account, or only
/// lamports (`ProgramFacts::escrow_ready`), and the escrow token account is not
/// read (`ProgramFacts::escrow_token_ready`). `token_program`: on Solana, the
/// program that owns the leg mint, read from the chain (`AssetFacts::token_program`).
pub fn s7(leg: &Leg, policy: &CompiledPolicy, observed: Option<&ContractObservation>, token_program: Option<Hash32>, locked: bool) -> Check {
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
            let asset = solana_escrow_asset(leg, token_program);
            crate::solana::parse_key(&leg.lock.contract)
                .zip(asset)
                .is_some_and(|(program, asset)| p.matches(&program, &leg.lock.lock_id, &asset, facts, locked))
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
                    .and_then(|(t, program)| crate::solana::escrow_token_address(&program, &leg.lock.lock_id, &t))
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

/// S10 (spec 3.2): each lock carries its own key, `lock_id = sha256(swap_id ‖ leg ‖
/// sender)`, with the sender bytes that its chain's lock sees (`Leg::sender_bytes`).
/// The two legs of a same-chain swap therefore have different keys. Checked at every
/// action, exits included: a claim or a refund names its lock by this key.
pub fn s10_lock_id(terms: &Terms) -> Check {
    for which in [LegName::A, LegName::B] {
        let leg = terms.leg(which);
        ensure(
            leg.derived_lock_id(which) == Some(leg.lock.lock_id),
            code::S10_LOCK,
            format!("leg {which:?}: lock_id is not sha256(swap_id ‖ leg ‖ sender)"),
        )?;
    }
    Ok(())
}

/// S10 at an entry action: both profiles' lock templates key each lock by `lock_id`
/// (spec 8.1), and the own lock binds the agreed `lock_id` only when the own `sender`
/// funds it. An EVM contract and a Solana program derive the key from the account
/// that calls or signs the lock. On Bitcoin the key derives from the `refund_key`,
/// which S6 requires to be an own key. Without a binding template, no consumed set
/// of counterparty locks (G14) replaces it, so the action is denied.
pub fn s10_lock_binding(terms: &Terms, role: Role, own: &[AccountId], pa: &ChainProfile, pb: &ChainProfile) -> Check {
    ensure(pa.lock_id_binding && pb.lock_id_binding, code::S10_LOCK, "a lock template does not bind lock_id")?;
    let own_leg = terms.leg(role.own_leg());
    if own_leg.chain.family() == Some(Family::Bitcoin) {
        return Ok(());
    }
    ensure(own.contains(&own_leg.sender), code::S10_LOCK, "the own lock is not funded by an own account")
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

/// Spec 3.2: leg B always has an absolute timelock. A relative one counts from the
/// confirmation of the responder's own lock, which S13 cannot observe (spec 7.3).
/// Checked at every entry action, so no party accepts such terms and the initiator
/// never locks leg A for them.
pub fn leg_b_absolute(terms: &Terms) -> Check {
    ensure(!terms.leg_b.lock.timelock.is_relative(), code::TIMELOCK, "leg B needs an absolute timelock")
}

/// The timelock of a lock that does not exist yet, for S11 at accept (spec 7.3). A
/// relative timelock counts from the confirmation of the lock, at the earliest in
/// the block after the observed `tip`. A later confirmation only moves the timelock
/// later, so S11 with this value holds for every lock that confirms after now. S13
/// checks again with the observed confirmation. An absolute timelock needs no tip.
pub fn unconfirmed_timelock(leg: &Leg, tip: Option<u64>) -> Option<Timelock> {
    if !leg.lock.timelock.is_relative() {
        return leg.refund_valid_from();
    }
    leg.refund_valid_from_confirmed(tip?.checked_add(1)?)
}

/// The timelock of an observed lock (spec 7.3), from the terms and the observation,
/// never from the adapter's `LockFacts::timelock`. A relative block count counts
/// from the block that confirms the lock: read at block `seen_at` with
/// `confirmations` (1 in that block), the lock is in block
/// `seen_at + 1 - confirmations`. `None` for an unconfirmed lock, for more
/// confirmations than blocks, and for an observation above the observed `tip`: the
/// tip is then stale, and S11 would count blocks that may have passed.
pub fn observed_timelock(leg: &Leg, facts: &LockFacts, seen_at: u64, tip: Option<u64>) -> Option<Timelock> {
    if !leg.lock.timelock.is_relative() {
        return leg.refund_valid_from();
    }
    if facts.confirmations == 0 || seen_at > tip? {
        return None;
    }
    leg.refund_valid_from_confirmed(seen_at.checked_add(1)?.checked_sub(facts.confirmations)?)
}

/// S13: the observed leg A lock reports exactly the timelock that the terms and its
/// observed confirmation give (`observed_timelock`). Another adapter value denies.
pub fn s13_timelock(facts: &LockFacts, ta: Timelock) -> Check {
    ensure(facts.timelock == ta, code::S13, "observed timelock differs from the terms and the confirmation")
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
        Some(n) => profile.band(n).ok_or_else(|| violation(code::BAND, format!("notional {n} above every band"))),
        None => profile.value_bands.last().ok_or_else(|| violation(code::BAND, "no band")),
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
        .map_err(|e| violation(code::S22, format!("{e:?}")))
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
                      "preimage_len": 32, "timelock": {"kind": "height", "value": 900_010}, "swap_id": crate::to_hex(&[2; 32]),
                      "lock_id": crate::to_hex(&[3; 32]) }
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
                      "preimage_len": 32, "timelock": {"kind": "time", "value": 1_900_000_000}, "swap_id": crate::to_hex(&[2; 32]),
                      "lock_id": crate::to_hex(&[3; 32]) }
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
        // Once the paying lock exists, its escrow token account must not be frozen. The
        // escrow is the PDA of the lock_id ([3; 32]), not of the swap id ([2; 32]).
        let token = crate::solana::TokenAccounts { mint, token_program: program };
        let escrow = crate::solana::escrow_address(&[2u8; 32], &[3u8; 32]).unwrap();
        let escrow_ata = crate::solana::associated_token_address(&escrow, &token).unwrap();
        let with_escrow = |e: Option<(Hash32, bool)>| with(&|f| if let ReceiverFacts::Solana { escrow, .. } = f { *escrow = e });
        assert!(s27(&leg, Payee::Receiver, Some(&with_escrow(Some((escrow_ata, false)))), Some(program), true).is_ok());
        let by_swap_id = crate::solana::associated_token_address(&crate::solana::escrow_address(&[2u8; 32], &[2u8; 32]).unwrap(), &token).unwrap();
        for e in [None, Some((escrow_ata, true)), Some(([9; 32], false)), Some((by_swap_id, false))] {
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

    /// Spec 4.1 (D7): `code::ALL` lists every code of `mod code`, each a fixed token
    /// that cannot carry a detail.
    #[test]
    fn reason_codes_are_fixed_tokens() {
        let source = include_str!("checks.rs");
        let start = source.find("pub mod code {").unwrap();
        let end = start + source[start..].find("\n}\n").unwrap();
        let declared: Vec<&str> = source[start..end]
            .lines()
            .filter_map(|l| l.trim().strip_prefix("pub const ")?.split_once(": &str = \"")?.1.strip_suffix("\";"))
            .collect();
        assert!(declared.len() >= 35, "{declared:?}");
        let mut all = code::ALL.to_vec();
        all.sort_unstable();
        let mut sorted = declared.clone();
        sorted.sort_unstable();
        assert_eq!(all, sorted, "code::ALL must list every code");
        for (i, c) in code::ALL.iter().enumerate() {
            assert!(!c.is_empty() && c.chars().all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || ch == '_'), "{c}");
            assert!(!code::ALL[..i].contains(c), "duplicate {c}");
        }
    }

    /// Spec 7.3 (G2): a relative leg A timelock counts from the observed
    /// confirmation of the lock, never from the adapter's value.
    #[test]
    fn relative_timelock_counts_from_the_observed_confirmation() {
        let mut leg: Leg = serde_json::from_value(serde_json::json!({
            "chain": reference::BITCOIN_MAINNET,
            "asset": "bip122:000000000019d6689c085ae165831e93/slip44:0",
            "amount": "1",
            "sender": "bip122:000000000019d6689c085ae165831e93:bc1pa",
            "receiver": "bip122:000000000019d6689c085ae165831e93:bc1pb",
            "refund_to": "bip122:000000000019d6689c085ae165831e93:bc1pa",
            "lock": { "contract": crate::bitcoin::TEMPLATE_ID, "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                      "preimage_len": 32, "timelock": {"kind": "relative_blocks", "value": 144}, "swap_id": crate::to_hex(&[2; 32]),
                      "lock_id": crate::to_hex(&[3; 32]) }
        }))
        .unwrap();
        let facts = |confirmations| LockFacts {
            contract: leg.lock.contract.clone(),
            lock_id: leg.lock.lock_id,
            hash_alg: HashAlg::Sha256,
            hashlock: leg.lock.hashlock,
            preimage_len_enforced: true,
            timelock: Timelock::Height(900_152),
            receiver: leg.receiver.clone(),
            refund_to: leg.refund_to.clone(),
            asset: leg.asset.clone(),
            net_amount: 1,
            confirmations,
            finalized: None,
            outpoint: None,
            script_pubkey: None,
        };
        // Read at 900,010 with 3 confirmations: in block 900,008, refund from 900,152.
        assert_eq!(observed_timelock(&leg, &facts(3), 900_010, Some(900_010)), Some(Timelock::Height(900_152)));
        // Read below the tip: the confirmation counts from the read height.
        assert_eq!(observed_timelock(&leg, &facts(1), 900_010, Some(900_012)), Some(Timelock::Height(900_154)));
        assert_eq!(observed_timelock(&leg, &facts(0), 900_010, Some(900_010)), None, "unconfirmed");
        assert_eq!(observed_timelock(&leg, &facts(3), 1, Some(900_010)), None, "more confirmations than blocks");
        assert_eq!(observed_timelock(&leg, &facts(3), 900_011, Some(900_010)), None, "read above a stale tip");
        assert_eq!(observed_timelock(&leg, &facts(3), 900_010, None), None, "tip unknown");
        // Before the lock exists: the earliest confirmation is the next block.
        assert_eq!(unconfirmed_timelock(&leg, Some(900_000)), Some(Timelock::Height(900_145)));
        assert_eq!(unconfirmed_timelock(&leg, None), None);
        assert!(s13_timelock(&facts(3), Timelock::Height(900_152)).is_ok());
        assert_eq!(s13_timelock(&facts(3), Timelock::Height(900_151)).unwrap_err().code, code::S13);
        // An absolute timelock needs no observation.
        leg.lock.timelock = TimelockSpec::Height(900_100);
        assert_eq!(observed_timelock(&leg, &facts(0), 0, None), Some(Timelock::Height(900_101)));
        assert_eq!(unconfirmed_timelock(&leg, None), Some(Timelock::Height(900_101)));
        // A relative time has no observed BIP 68 base.
        leg.lock.timelock = TimelockSpec::RelativeSeconds(3_600);
        assert_eq!(observed_timelock(&leg, &facts(3), 900_010, Some(900_010)), None);
        assert_eq!(unconfirmed_timelock(&leg, Some(900_000)), None);
    }

    /// A Solana leg of `asset` in the test HTLC `[2; 32]` with swap id `[2; 32]`, and a
    /// policy that pins that program with `solana::tests::HTLC_CODE`.
    fn solana_leg(asset: &str) -> (Leg, CompiledPolicy) {
        let chain = reference::SOLANA_MAINNET;
        let lock_id = [3u8; 32];
        let program_b58 = bs58::encode([2u8; 32]).into_string();
        let owner = bs58::encode([4u8; 32]).into_string();
        let leg: Leg = serde_json::from_value(serde_json::json!({
            "chain": chain,
            "asset": format!("{chain}/{asset}"),
            "amount": "1",
            "sender": format!("{chain}:{owner}"),
            "receiver": format!("{chain}:{owner}"),
            "refund_to": format!("{chain}:{owner}"),
            "lock": { "contract": program_b58, "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                      "preimage_len": 32, "timelock": {"kind": "time", "value": 1_900_000_000}, "swap_id": crate::to_hex(&[2u8; 32]),
                      "lock_id": crate::to_hex(&lock_id) }
        }))
        .unwrap();
        let mut doc = crate::dsl::tests::policy_json(crate::dsl::tests::example_rule());
        let pin = crate::solana::tests::htlc_pin(&[2; 32]);
        doc["chains"][chain] = serde_json::json!({
            "profile_hash": crate::to_hex(&reference::solana().hash()),
            "contracts": [{"solana": serde_json::to_value(&pin).unwrap()}]
        });
        let policy = crate::dsl::validate_policy(&doc.to_string(), &crate::dsl::tests::profiles()).unwrap();
        (leg, policy)
    }

    /// S7 on Solana (spec 8.4, D3): a lock that exists needs the program-owned escrow
    /// with the reference discriminator; before the own lock, no escrow account.
    #[test]
    fn s7_solana_escrow_state() {
        use crate::solana::tests::{htlc_facts, locked_escrow};
        use crate::solana::EscrowAccount;
        let (program, swap_id, lock_id) = ([2u8; 32], [2u8; 32], [3u8; 32]);
        let (leg, policy) = solana_leg("slip44:501");
        let escrow = locked_escrow(&program);
        let facts = |e: Option<EscrowAccount>| ContractObservation::Solana(htlc_facts(&program, &lock_id, e));
        let other = EscrowAccount { discriminator: Some([0; 8]), ..escrow.clone() };
        let short = EscrowAccount { data_len: 7, discriminator: None, ..escrow.clone() };
        // An observed lock (responder's S13, reveal, claim).
        assert!(s7(&leg, &policy, Some(&facts(Some(escrow.clone()))), None, true).is_ok());
        for e in [Some(other), Some(short), None] {
            let v = s7(&leg, &policy, Some(&facts(e.clone())), None, true).unwrap_err();
            assert_eq!(v.code, code::S7, "{e:?}");
        }
        // The own lock, before the escrow exists. Lamports at the address do not block it.
        assert!(s7(&leg, &policy, Some(&facts(None)), None, false).is_ok());
        let funded = EscrowAccount { owner: [0; 32], data_len: 0, discriminator: None };
        assert!(s7(&leg, &policy, Some(&facts(Some(funded.clone()))), None, false).is_ok());
        assert_eq!(s7(&leg, &policy, Some(&facts(Some(funded))), None, true).unwrap_err().code, code::S7);
        assert_eq!(s7(&leg, &policy, Some(&facts(Some(escrow))), None, false).unwrap_err().code, code::S7);
        // Unknown contract facts fail closed.
        assert!(s7(&leg, &policy, None, None, false).is_err());
        // The PDA of the swap id is not the escrow of this lock (spec 8.2, G4).
        let at_swap_id = ContractObservation::Solana(htlc_facts(&program, &swap_id, None));
        assert_eq!(s7(&leg, &policy, Some(&at_swap_id), None, false).unwrap_err().code, code::S7);
    }

    /// S10 (spec 3.2, G4): each lock carries `sha256(swap_id ‖ leg ‖ sender)` with the
    /// sender bytes of its family. The legs of one swap on one chain get two keys.
    #[test]
    fn s10_lock_id_is_the_derivation() {
        use crate::types::{lock_id, Terms};
        let chain = reference::ETHEREUM_MAINNET;
        let (swap_id, a, b) = ([0x51u8; 32], [0xaau8; 20], [0xbbu8; 20]);
        let leg = |sender: [u8; 20], receiver: [u8; 20], which: LegName| -> Leg {
            let sender_hex = crate::to_hex(&sender);
            let lock_id = lock_id(&swap_id, which, &sender);
            serde_json::from_value(serde_json::json!({
                "chain": chain,
                "asset": format!("{chain}/slip44:60"),
                "amount": "1",
                "sender": format!("{chain}:0x{sender_hex}"),
                "receiver": format!("{chain}:0x{}", crate::to_hex(&receiver)),
                "refund_to": format!("{chain}:0x{sender_hex}"),
                "lock": { "contract": format!("0x{}", "33".repeat(20)), "hash_alg": "sha256", "hashlock": crate::to_hex(&[1; 32]),
                          "preimage_len": 32, "timelock": {"kind": "time", "value": 1_900_000_000}, "swap_id": crate::to_hex(&swap_id),
                          "lock_id": crate::to_hex(&lock_id) }
            }))
            .unwrap()
        };
        let terms = Terms { swap_id, initiator: "i".into(), responder: "r".into(), leg_a: leg(a, b, LegName::A), leg_b: leg(b, a, LegName::B) };
        // Known answer: sha256(0x51 x 32 ‖ 0x41 ‖ 0xaa x 20).
        assert_eq!(crate::to_hex(&terms.leg_a.lock.lock_id), "8566ed2b109335330d2ff5e10291adcf1c9f1624705a2b43e659a6671ea9665c");
        assert!(s10_lock_id(&terms).is_ok());
        assert_ne!(terms.leg_a.lock.lock_id, terms.leg_b.lock.lock_id);
        let with = |edit: &dyn Fn(&mut Terms)| {
            let mut t = terms.clone();
            edit(&mut t);
            s10_lock_id(&t).map_err(|v| v.code)
        };
        // The swap id alone, the other leg's key, the other leg byte, another sender.
        assert_eq!(with(&|t| t.leg_b.lock.lock_id = swap_id), Err(code::S10_LOCK));
        assert_eq!(with(&|t| t.leg_b.lock.lock_id = t.leg_a.lock.lock_id), Err(code::S10_LOCK));
        assert_eq!(with(&|t| t.leg_a.lock.lock_id = lock_id(&swap_id, LegName::B, &a)), Err(code::S10_LOCK));
        assert_eq!(with(&|t| t.leg_a.lock.lock_id = lock_id(&swap_id, LegName::A, &b)), Err(code::S10_LOCK));
        // A lock of another swap.
        assert_eq!(with(&|t| t.leg_a.lock.swap_id = [0x52; 32]), Err(code::S10_LOCK));
        // Bitcoin: the sender bytes are the 32-byte refund_key; without it, no lock_id.
        let mut btc = terms.clone();
        btc.leg_a.chain = ChainId::parse(reference::BITCOIN_MAINNET).unwrap();
        btc.leg_a.lock.refund_key = Some([0x0a; 32]);
        btc.leg_a.lock.lock_id = lock_id(&swap_id, LegName::A, &[0x0a; 32]);
        assert!(s10_lock_id(&btc).is_ok());
        btc.leg_a.lock.refund_key = Some([0x0b; 32]);
        assert_eq!(s10_lock_id(&btc).unwrap_err().code, code::S10_LOCK);
        btc.leg_a.lock.refund_key = None;
        assert_eq!(s10_lock_id(&btc).unwrap_err().code, code::S10_LOCK);
        // The own lock must be funded by an own account (EVM, Solana), and both
        // profiles must bind lock_id.
        let (eth, btc_profile) = (reference::ethereum(), reference::bitcoin(reference::BITCOIN_MAINNET));
        let own = [terms.leg_b.sender.clone()];
        assert!(s10_lock_binding(&terms, Role::Responder, &own, &eth, &eth).is_ok());
        assert_eq!(s10_lock_binding(&terms, Role::Initiator, &own, &eth, &eth).unwrap_err().code, code::S10_LOCK);
        let unbound = ChainProfile { lock_id_binding: false, ..reference::ethereum() };
        assert_eq!(s10_lock_binding(&terms, Role::Responder, &own, &unbound, &eth).unwrap_err().code, code::S10_LOCK);
        assert_eq!(s10_lock_binding(&terms, Role::Responder, &own, &eth, &unbound).unwrap_err().code, code::S10_LOCK);
        btc.leg_a.lock.refund_key = Some([0x0a; 32]);
        btc.leg_a.sender = AccountId::parse(&format!("{}:bc1pother", reference::BITCOIN_MAINNET)).unwrap();
        assert!(s10_lock_binding(&btc, Role::Initiator, &[], &btc_profile, &eth).is_ok(), "Bitcoin binds the refund_key, which S6 checks");
    }


    /// S7 on Solana (spec 8.4, G21): the loader, the ProgramData account and the
    /// pinned code hash; for a token lock that exists, the escrow token account under
    /// the token program of the mint, read from the chain.
    #[test]
    fn s7_solana_program_and_escrow_token_account() {
        use crate::solana::tests::{htlc_facts, locked_escrow};
        use crate::solana::{escrow_address, escrow_token_address, key, ProgramDataAccount, TokenAccount, TokenAccounts, TOKEN_2022_PROGRAM, TOKEN_PROGRAM};
        let (program, lock_id) = ([2u8; 32], [3u8; 32]);
        let mint_b58 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
        let (leg, policy) = solana_leg(&format!("token:{mint_b58}"));
        let usdc = TokenAccounts { mint: crate::solana::parse_key(mint_b58).unwrap(), token_program: key(TOKEN_PROGRAM) };
        let account = TokenAccount {
            address: escrow_token_address(&program, &lock_id, &usdc).unwrap(),
            program: usdc.token_program,
            mint: usdc.mint,
            owner: escrow_address(&program, &lock_id).unwrap(),
            initialized: true,
        };
        let locked = |t: Option<TokenAccount>| ContractObservation::Solana(crate::solana::ProgramFacts { escrow_token: t, ..htlc_facts(&program, &lock_id, Some(locked_escrow(&program))) });
        let tp = Some(usdc.token_program);
        assert!(s7(&leg, &policy, Some(&locked(Some(account.clone()))), tp, true).is_ok());
        assert_eq!(s7(&leg, &policy, Some(&locked(None)), tp, true).unwrap_err().code, code::S7);
        assert_eq!(s7(&leg, &policy, Some(&locked(Some(TokenAccount { owner: [9; 32], ..account.clone() }))), tp, true).unwrap_err().code, code::S7);
        // A Token-2022 mint: its escrow token account under Token-2022 passes.
        let t22 = TokenAccounts { token_program: key(TOKEN_2022_PROGRAM), ..usdc };
        let t22_account = TokenAccount { address: escrow_token_address(&program, &lock_id, &t22).unwrap(), program: t22.token_program, ..account.clone() };
        assert!(s7(&leg, &policy, Some(&locked(Some(t22_account))), Some(t22.token_program), true).is_ok());
        // The token program comes from the mint: unknown, another program, or not a token program.
        for other in [None, Some(key(TOKEN_2022_PROGRAM)), Some([9; 32])] {
            assert_eq!(s7(&leg, &policy, Some(&locked(Some(account.clone()))), other, true).unwrap_err().code, code::S7, "{other:?}");
        }
        // A mint program other than SPL Token or Token-2022 has no escrow token account
        // that S7 accepts, even when the facts agree with it.
        let fake = TokenAccounts { token_program: [9; 32], ..usdc };
        let fake_account = TokenAccount { address: escrow_token_address(&program, &lock_id, &fake).unwrap(), program: fake.token_program, ..account.clone() };
        assert_eq!(s7(&leg, &policy, Some(&locked(Some(fake_account))), Some(fake.token_program), true).unwrap_err().code, code::S7);
        // Before the own lock the escrow token account is not read.
        let before = ContractObservation::Solana(htlc_facts(&program, &lock_id, None));
        assert!(s7(&leg, &policy, Some(&before), tp, false).is_ok());
        // The program identity of spec 8.4.
        let ContractObservation::Solana(good) = locked(Some(account)) else { unreachable!() };
        let data = good.programdata.clone().unwrap();
        let bad = [
            crate::solana::ProgramFacts { loader: key("BPFLoader2111111111111111111111111111111111"), ..good.clone() },
            crate::solana::ProgramFacts { programdata_address: Some([9; 32]), ..good.clone() },
            crate::solana::ProgramFacts { programdata: Some(ProgramDataAccount { code_hash: [9; 32], ..data.clone() }), ..good.clone() },
            crate::solana::ProgramFacts { programdata: Some(ProgramDataAccount { upgrade_authority: Some([9; 32]), ..data }), ..good.clone() },
        ];
        for (i, f) in bad.into_iter().enumerate() {
            assert_eq!(s7(&leg, &policy, Some(&ContractObservation::Solana(f)), tp, true).unwrap_err().code, code::S7, "case {i}");
        }
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
