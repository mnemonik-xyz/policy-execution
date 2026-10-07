//! The authorization pipeline (implementation spec 4.7).
//!
//! Entry actions: obligatory checks → facts → verified evaluator → a warrant on
//! `Allow`, a decision record on `Deny` or `Ask`. Exit actions: structural checks
//! only; the evaluator is never called (S18); a failure halts the signer.

use crate::caip::{AssetId, ChainId, Family};
use crate::checks::{self, code, AssetFacts, ContractObservation, LockFacts, Payee, ReceiverFacts, Runtime, Violation};
use crate::dsl::CompiledPolicy;
use crate::facts::{self, EvidenceMethod, FactRecord, Observed, PriceReport, Provenance};
use crate::ledger::LedgerState;
use crate::profile::{ChainProfile, ProfileSet};
use crate::secret::preimage_opens;
use crate::solana::LookupTables;
use crate::tx::{self, BindContext, OwnAccounts, ProposedTx};
use crate::types::{Action, Leg, LegName, Role, Terms};
use crate::verified::{self, ChainNow, Decision, ListCheck, LockObs, PeriodSpent, SwapFacts3, SwapRule, Timelock};
use crate::warrant::{DecisionRecord, EvaluatorId, RecordDecision, SwapWarrant, TxBinding, DECISION_PROTOCOL, PROTOCOL};
use crate::Hash32;
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// A signed identity credential of the counterparty; the signature is verified
/// before this value exists.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IdentityCredential {
    pub identity: String,
    pub authority: String,
    pub signature_ok: bool,
    pub valid_until: u64,
}

/// A signed snapshot of a deny list, and whether it contains the counterparty.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListSnapshot {
    pub list_hash: Hash32,
    pub authority: String,
    pub signature_ok: bool,
    pub contains_counterparty: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CollateralReport {
    pub bps: u64,
    pub authority: String,
    pub signature_ok: bool,
}

/// The ACCEPT message as the signer runtime verified it (spec 3.5): the signer's
/// own ACCEPT, or the counterparty's ACCEPT after the inner signature check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptEvidence {
    /// `blake3` of the signed ACCEPT message.
    pub inner_sig_hash: Hash32,
    /// The `terms_hash` that the signed ACCEPT carries.
    pub terms_hash: Hash32,
    /// The inner signature verifies under the author's identity key.
    pub signature_ok: bool,
}

/// Everything the signer observed, each with its provenance.
#[derive(Clone, Debug, Default)]
pub struct Observations {
    pub accept: Option<AcceptEvidence>,
    pub tips: BTreeMap<ChainId, Observed<u64>>,
    pub locks: BTreeMap<LegName, Observed<LockFacts>>,
    pub contracts: BTreeMap<LegName, Observed<ContractObservation>>,
    pub assets: BTreeMap<AssetId, Observed<AssetFacts>>,
    pub prices: Vec<PriceReport>,
    pub identity: Option<IdentityCredential>,
    pub list: Option<ListSnapshot>,
    pub collateral: Option<CollateralReport>,
    pub fee_reserves: BTreeMap<ChainId, u128>,
    /// Solana address lookup tables that the proposed message uses: table address
    /// and its addresses in order, read from the leg chain.
    pub lookup_tables: BTreeMap<Hash32, Observed<Vec<Hash32>>>,
    /// Whether each own payee can receive the leg asset now (S27).
    pub receivers: BTreeMap<(LegName, Payee), Observed<ReceiverFacts>>,
}

/// What the agent proposes. Nothing here is a fact: terms must hash to the
/// `terms_hash` of the verified ACCEPT (`Observations::accept`) and are checked
/// against chain observations; the transaction is decoded.
#[derive(Clone, Debug)]
pub struct Request {
    pub action: Action,
    pub role: Role,
    pub terms: Terms,
    pub tx: Option<ProposedTx>,
    /// Reveal: from the signer's secret store. Claim: as observed on the chain.
    pub preimage: Option<Hash32>,
    pub nonce: [u8; 16],
    pub prev_warrant: Option<Hash32>,
    pub valid_for_secs: u64,
}

pub struct Env<'a> {
    pub policy: &'a CompiledPolicy,
    pub profiles: &'a ProfileSet,
    pub obs: &'a Observations,
    pub ledger: &'a LedgerState,
    pub runtime: &'a Runtime,
    pub own: &'a OwnAccounts,
    /// The signer's real time (Unix seconds).
    pub now_real: u64,
    /// Hash of the evaluator build that runs here (S23).
    pub evaluator_build: Hash32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Warrant(Box<SwapWarrant>),
    /// A decision record, and the failed check when a check decided. The violation
    /// is a local diagnostic: it is not part of the record, and its detail can
    /// contain text from the proposed terms. Never sign, store or anchor it.
    Record(Box<DecisionRecord>, Option<Violation>),
}

impl Outcome {
    pub fn is_allow(&self) -> bool {
        matches!(self, Outcome::Warrant(_))
    }

    pub fn record_decision(&self) -> Option<RecordDecision> {
        match self {
            Outcome::Record(r, _) => Some(r.decision),
            Outcome::Warrant(_) => None,
        }
    }

    /// The failed check with its detail, for local logs only.
    pub fn diagnostic(&self) -> Option<&Violation> {
        match self {
            Outcome::Record(_, v) => v.as_ref(),
            Outcome::Warrant(_) => None,
        }
    }

    pub fn reasons(&self) -> &[String] {
        match self {
            Outcome::Warrant(w) => &w.reasons,
            Outcome::Record(r, _) => &r.reasons,
        }
    }
}

/// Facts collected while the pipeline runs, and the weakest chain evidence used.
struct Collected<'e> {
    env: &'e Env<'e>,
    records: Vec<FactRecord>,
    weakest: Option<EvidenceMethod>,
}

impl<'e> Collected<'e> {
    fn resolve<T: Clone + PartialEq>(&mut self, name: &str, obs: Option<&Observed<T>>) -> Option<T> {
        self.resolve_at(name, obs).map(|(value, _)| value)
    }

    /// Like `resolve`, with the height of the block that the providers agree on.
    fn resolve_at<T: Clone + PartialEq>(&mut self, name: &str, obs: Option<&Observed<T>>) -> Option<(T, u64)> {
        let (value, prov) = obs?.resolve(self.env.policy.policy.quorum)?;
        let height = match &prov {
            Provenance::Chain { height, .. } => *height,
            _ => 0,
        };
        if let Provenance::Chain { method, .. } = &prov {
            self.weakest = Some(self.weakest.map_or(*method, |w| w.min(*method)));
        }
        if !self.records.iter().any(|r| r.name == name) {
            self.records.push(FactRecord::new(name, Value::Bool(true), Some(prov)));
        }
        Some((value, height))
    }

    fn record(&mut self, name: &str, value: Value, prov: Option<Provenance>) {
        self.records.push(FactRecord::new(name, value, prov));
    }

    fn chain_now(&mut self, chain: &ChainId, timelock: Timelock) -> Option<ChainNow> {
        let now_real = self.env.now_real;
        let tip = self.resolve(&format!("tip:{chain}"), self.env.obs.tips.get(chain));
        match timelock {
            Timelock::Time(_) => Some(ChainNow { tip_height: tip.unwrap_or(0), now_real }),
            Timelock::Height(_) => tip.map(|tip_height| ChainNow { tip_height, now_real }),
        }
    }
}

fn opt<T: Into<Value>>(v: Option<T>) -> Value {
    v.map_or(Value::Null, Into::into)
}

/// Decide one action.
pub fn authorize(req: &Request, env: &Env) -> Outcome {
    let mut c = Collected { env, records: Vec::new(), weakest: None };
    let terms_hash = match req.terms.hash() {
        Ok(h) => h,
        Err(e) => return rejected(req, env, [0; 32], halt_or_deny(req), checks::violation(code::TERMS, e.to_string()), vec![]),
    };
    // No warrant for terms that the signed ACCEPT does not cover.
    let inner_sig_hash = match accepted(env, &terms_hash) {
        Ok(h) => h,
        Err(v) => return rejected(req, env, terms_hash, halt_or_deny(req), v, vec![]),
    };
    if req.action.is_exit() {
        match exit(req, env, &mut c) {
            Ok(binding) => warrant(req, env, terms_hash, inner_sig_hash, binding, &[code::EXIT], c.records),
            Err(v) => rejected(req, env, terms_hash, RecordDecision::Halt, v, c.records),
        }
    } else {
        match entry(req, env, &mut c) {
            Ok((Decision::Allow, binding)) => warrant(req, env, terms_hash, inner_sig_hash, binding, &[code::ALLOW], c.records),
            Ok((Decision::Ask, _)) => record(req, env, terms_hash, RecordDecision::Ask, &[code::ASK], None, c.records),
            Ok((Decision::Deny, _)) => record(req, env, terms_hash, RecordDecision::Deny, &[code::DENY], None, c.records),
            Err(v) => rejected(req, env, terms_hash, RecordDecision::Deny, v, c.records),
        }
    }
}

/// A failed check before the action-specific checks: an exit halts, an entry is denied.
fn halt_or_deny(req: &Request) -> RecordDecision {
    if req.action.is_exit() { RecordDecision::Halt } else { RecordDecision::Deny }
}

/// Solana chain facts of a claim or reveal binding: the lookup tables and the
/// token program of the leg asset. Nothing on other chains.
fn solana_facts(c: &mut Collected, leg: &Leg, ctx: &mut BindContext) {
    if leg.chain.family() != Some(Family::Solana) {
        return;
    }
    ctx.lookup_tables = lookup_tables(c, leg);
    if !leg.asset.is_native() {
        let env = c.env;
        ctx.token_program = c.resolve(&format!("asset:{}", leg.asset), env.obs.assets.get(&leg.asset)).and_then(|a| a.token_program);
    }
}

/// S27 for one own payee, from the receiver facts that the signer observed.
/// `locked`: the lock that pays this payee exists now.
fn receivable(c: &mut Collected, leg_name: LegName, leg: &Leg, payee: Payee, asset: Option<&AssetFacts>, locked: bool) -> Result<(), Violation> {
    let env = c.env;
    let needs_facts = leg.chain.family() != Some(Family::Bitcoin) && !leg.asset.is_native();
    let facts = if needs_facts {
        // "Can receive now": the facts come from the observed tip of the leg chain
        // or a later block. A stale report, or an unknown tip, is no fact.
        let tip = c.resolve(&format!("tip:{}", leg.chain), env.obs.tips.get(&leg.chain));
        let observed = c.resolve_at(&format!("receiver:{leg_name:?}:{payee:?}"), env.obs.receivers.get(&(leg_name, payee)));
        observed.filter(|(_, height)| tip.is_some_and(|t| *height >= t)).map(|(facts, _)| facts)
    } else {
        None
    };
    checks::s27(leg, payee, facts.as_ref(), asset.and_then(|a| a.token_program), locked)
}

/// The observed tip height of a Bitcoin leg chain, for the `nLockTime` checks.
fn bitcoin_tip(c: &mut Collected, leg: &Leg) -> Option<u64> {
    if leg.chain.family() != Some(Family::Bitcoin) {
        return None;
    }
    let env = c.env;
    c.resolve(&format!("tip:{}", leg.chain), env.obs.tips.get(&leg.chain))
}

/// The observed lookup tables, for a Solana leg only.
fn lookup_tables(c: &mut Collected, leg: &Leg) -> LookupTables {
    let mut tables = LookupTables::new();
    if leg.chain.family() != Some(Family::Solana) {
        return tables;
    }
    let env = c.env;
    for (table, obs) in &env.obs.lookup_tables {
        if let Some(addresses) = c.resolve(&format!("lookup_table:{}", crate::to_hex(table)), Some(obs)) {
            tables.insert(*table, addresses);
        }
    }
    tables
}

/// The verified ACCEPT must exist and carry exactly these terms.
fn accepted(env: &Env, terms_hash: &Hash32) -> Result<Hash32, Violation> {
    match &env.obs.accept {
        None => Err(checks::violation(code::ACCEPT, "no verified ACCEPT message")),
        Some(a) if !a.signature_ok => Err(checks::violation(code::ACCEPT, "ACCEPT signature does not verify")),
        Some(a) if &a.terms_hash != terms_hash => Err(checks::violation(code::ACCEPT, "terms differ from the signed ACCEPT")),
        Some(a) => Ok(a.inner_sig_hash),
    }
}

fn warrant(
    req: &Request,
    env: &Env,
    terms_hash: Hash32,
    inner_sig_hash: Hash32,
    binding: Option<TxBinding>,
    reasons: &[&'static str],
    facts: Vec<FactRecord>,
) -> Outcome {
    let leg = req.action.leg(req.role).map(|l| req.terms.leg(l).clone());
    Outcome::Warrant(Box::new(SwapWarrant {
        protocol: PROTOCOL.into(),
        action: req.action,
        swap_id: req.terms.swap_id,
        terms_hash,
        inner_sig_hash,
        leg,
        tx_binding: binding,
        facts,
        policy_hash: env.policy.policy_hash,
        policy_version: env.policy.policy.version,
        evaluator_id: EvaluatorId::Build(env.policy.policy.evaluator_id),
        decision: "allow".into(),
        reasons: codes(reasons),
        valid_after: env.now_real,
        valid_until: env.now_real.saturating_add(req.valid_for_secs),
        nonce: req.nonce,
        prev_warrant: req.prev_warrant,
    }))
}

/// `reasons` holds fixed codes only (spec 4.1): `&'static str` values from
/// `checks::code`, never text built from the request.
fn codes(reasons: &[&'static str]) -> Vec<String> {
    reasons.iter().map(|r| (*r).to_owned()).collect()
}

/// A record for a failed check: its code (and the code of the inner check that it
/// wraps) go into `reasons`, the detail stays in the local diagnostic.
fn rejected(req: &Request, env: &Env, terms_hash: Hash32, decision: RecordDecision, v: Violation, facts: Vec<FactRecord>) -> Outcome {
    let reasons: Vec<&'static str> = std::iter::once(v.code).chain(v.cause).collect();
    record(req, env, terms_hash, decision, &reasons, Some(v), facts)
}

fn record(
    req: &Request,
    env: &Env,
    terms_hash: Hash32,
    decision: RecordDecision,
    reasons: &[&'static str],
    diagnostic: Option<Violation>,
    facts: Vec<FactRecord>,
) -> Outcome {
    Outcome::Record(Box::new(DecisionRecord {
        protocol: DECISION_PROTOCOL.into(),
        action: req.action,
        swap_id: req.terms.swap_id,
        terms_hash,
        decision,
        reasons: codes(reasons),
        facts,
        policy_hash: env.policy.policy_hash,
        policy_version: env.policy.policy.version,
        evaluator_id: EvaluatorId::Build(env.policy.policy.evaluator_id),
        at: env.now_real,
        nonce: req.nonce,
        prev_warrant: req.prev_warrant,
    }), diagnostic)
}

fn need_tx(req: &Request) -> Result<&ProposedTx, Violation> {
    req.tx.as_ref().ok_or_else(|| checks::violation(code::S24, "no transaction proposed"))
}

fn s24(r: Result<TxBinding, String>) -> Result<TxBinding, Violation> {
    r.map_err(|detail| checks::violation(code::S24, detail))
}

/// Observed lock and contract identity of one leg (S5–S10, S7), and the height of
/// the block at which the providers read the lock.
fn observed_leg(c: &mut Collected, leg_name: LegName, leg: &Leg) -> Result<(LockFacts, u64), Violation> {
    let env = c.env;
    let (facts, seen_at) = c
        .resolve_at(&format!("lock:{leg_name:?}"), env.obs.locks.get(&leg_name))
        .ok_or_else(|| checks::violation(code::S14, format!("lock of leg {leg_name:?} not observed with agreeing evidence")))?;
    let expected = leg.refund_valid_from();
    checks::observed_lock(leg, &facts, expected)?;
    let contract = if leg.chain.family() == Some(Family::Bitcoin) {
        // On Bitcoin the observed output script is the only on-chain proof of H, both
        // leaf keys and T. Without it, or without its outpoint, nothing is proved.
        let (Some(spk), Some(_)) = (&facts.script_pubkey, &facts.outpoint) else {
            return Err(checks::violation(code::S7, format!("observed lock of leg {leg_name:?} has no output script or outpoint")));
        };
        Some(ContractObservation::Bitcoin { script_pubkey: spk.clone() })
    } else {
        c.resolve(&format!("contract:{leg_name:?}"), env.obs.contracts.get(&leg_name))
    };
    checks::s7(leg, env.policy, contract.as_ref(), true)?;
    Ok((facts, seen_at))
}

fn price(env: &Env, asset: &AssetId) -> Option<u128> {
    let p = &env.policy.policy;
    env.obs
        .prices
        .iter()
        .filter(|r| &r.asset == asset)
        .find_map(|r| r.accept(&p.ref_ccy, &p.authorities.oracle, &p.oracle, env.now_real))
}

fn entry(req: &Request, env: &Env, c: &mut Collected) -> Result<(Decision, Option<TxBinding>), Violation> {
    let (terms, role, action) = (&req.terms, req.role, req.action);
    let policy = env.policy;
    if !action.allowed_for(role) {
        return Err(checks::violation(code::ROLE, format!("{action:?} is not an action of the {role:?}")));
    }
    checks::s23(policy, &env.evaluator_build)?;
    checks::s22(env.ledger, policy)?;
    let pa = checks::profile_for(&terms.leg_a.chain, policy, env.profiles)?;
    let pb = checks::profile_for(&terms.leg_b.chain, policy, env.profiles)?;
    checks::s1(terms)?;
    checks::s2(terms, pa, pb)?;
    checks::s3(terms)?;
    checks::s10(terms, env.ledger, action == Action::Accept)?;
    if action == Action::Accept {
        checks::s4(terms, env.ledger)?;
    }
    checks::s5_s6_terms(terms, role, &env.own.accounts, &env.own.bitcoin_keys)?;
    checks::timelock_form(&terms.leg_a)?;
    checks::timelock_form(&terms.leg_b)?;
    checks::leg_b_absolute(terms)?;

    let own_name = role.own_leg();
    let their_name = role.counterparty_leg();
    let own_leg = terms.leg(own_name);
    let their_leg = terms.leg(their_name);
    let (own_profile, their_profile) = match role {
        Role::Initiator => (pa, pb),
        Role::Responder => (pb, pa),
    };

    // Assets (S8, S9) and prices (spec 5.4).
    let own_asset = c.resolve(&format!("asset:{}", own_leg.asset), env.obs.assets.get(&own_leg.asset));
    let their_asset = c.resolve(&format!("asset:{}", their_leg.asset), env.obs.assets.get(&their_leg.asset));
    checks::s8_flags(&[own_asset.as_ref(), their_asset.as_ref()].into_iter().flatten().collect::<Vec<_>>())?;
    let value_e8 = |leg: &Leg, asset: &Option<AssetFacts>| {
        asset.as_ref().and_then(|a| facts::value_e8(leg.amount, a.decimals, price(env, &leg.asset)?))
    };
    let give_e8 = value_e8(own_leg, &own_asset);
    let take_e8 = value_e8(their_leg, &their_asset);
    let whole = |v: Option<u128>| v.and_then(|v| u64::try_from(v.div_ceil(100_000_000)).ok());
    // Known only when both legs have a value: one unknown leg can be the larger one.
    let notional = whole(give_e8).zip(whole(take_e8)).map(|(g, t)| g.max(t));
    let deviation = give_e8.zip(take_e8).and_then(|(g, t)| facts::deviation_bps(g, t));

    // Timelocks as agreed (absolute) or as observed.
    let ta_terms = terms.leg_a.refund_valid_from();
    let tb_terms = terms.leg_b.refund_valid_from();
    let mut ta = ta_terms;
    let mut counterparty_lock = None;
    let mut binding = None;
    let margin = policy.policy.margin_secs;

    match action {
        Action::Accept => {
            // S7 before any lock exists: both legs name a pinned contract.
            for leg in [own_leg, their_leg] {
                if policy.pin_for(&leg.chain, &leg.lock.contract).is_none() {
                    return Err(checks::violation(code::S7, format!("{} on {} is not pinned", leg.lock.contract, leg.chain)));
                }
            }
            // No leg A lock exists yet: a relative T_A counts from the earliest block
            // that can confirm it (spec 7.3). S13 checks it again.
            let tip_a = c.resolve(&format!("tip:{}", terms.leg_a.chain), env.obs.tips.get(&terms.leg_a.chain));
            let ta_now = checks::unconfirmed_timelock(&terms.leg_a, tip_a);
            if role == Role::Responder {
                let (Some(ta_now), Some(tb)) = (ta_now, tb_terms) else {
                    return Err(checks::violation(code::S11, "timelock or chain tip unknown"));
                };
                let (now_a, now_b) = (c.chain_now(&terms.leg_a.chain, ta_now), c.chain_now(&terms.leg_b.chain, tb));
                let (Some(now_a), Some(now_b)) = (now_a, now_b) else {
                    return Err(checks::violation(code::S11, "chain tip unknown"));
                };
                checks::s11(ta_now, now_a, pa, tb, now_b, pb, margin)?;
            }
            ta = ta_now;
        }
        Action::Lock => {
            let own_contract = c.resolve(&format!("contract:{own_name:?}"), env.obs.contracts.get(&own_name));
            // The own lock does not exist yet: on Solana its escrow address holds no account, or only lamports.
            checks::s7(own_leg, policy, own_contract.as_ref(), false)?;
            checks::s15(&env.obs.fee_reserves, own_profile, their_profile)?;
            checks::s16(env.runtime)?;
            checks::s17(own_profile, env.runtime)?;
            checks::s19(own_profile)?;
            if role == Role::Initiator {
                // The own leg A lock confirms after now: a relative T_A counts from the
                // earliest block that can confirm it (spec 7.3), as at accept.
                let tip_a = c.resolve(&format!("tip:{}", terms.leg_a.chain), env.obs.tips.get(&terms.leg_a.chain));
                ta = checks::unconfirmed_timelock(&terms.leg_a, tip_a);
            }
            if role == Role::Responder {
                // S13: the initiator lock is final and leaves room for T_B.
                let (facts, seen_at) = observed_leg(c, LegName::A, &terms.leg_a)
                    .map_err(|v| Violation { code: code::S13, cause: Some(v.code), detail: v.detail })?;
                let method = c.weakest.unwrap_or(EvidenceMethod::SingleRpc);
                checks::s14(&facts, method, checks::band(pa, notional)?)?;
                let Some(tb) = tb_terms else {
                    return Err(checks::violation(code::TIMELOCK, "the second leg needs an absolute timelock"));
                };
                // T_A from the terms and the observed confirmation of the leg A lock
                // (spec 7.3), never the adapter's value.
                let tip_a = c.resolve(&format!("tip:{}", terms.leg_a.chain), env.obs.tips.get(&terms.leg_a.chain));
                let Some(ta_lock) = checks::observed_timelock(&terms.leg_a, &facts, seen_at, tip_a) else {
                    return Err(checks::violation(code::S13, "leg A timelock unknown: no consistent observed confirmation"));
                };
                checks::s13_timelock(&facts, ta_lock)?;
                let (now_a, now_b) = (c.chain_now(&terms.leg_a.chain, ta_lock), c.chain_now(&terms.leg_b.chain, tb));
                let (Some(now_a), Some(now_b)) = (now_a, now_b) else {
                    return Err(checks::violation(code::S13, "chain tip unknown"));
                };
                checks::s11(ta_lock, now_a, pa, tb, now_b, pb, margin)
                    .map_err(|v| Violation { code: code::S13, cause: Some(v.code), detail: v.detail })?;
                ta = Some(ta_lock);
                counterparty_lock = Some(LockObs { chain: terms.leg_a.chain.id(), depth: Some(facts.confirmations), finalized: facts.finalized });
            }
            // S27: the own receiver on the counterparty leg and the own refund account
            // can both receive the asset now.
            // The counterparty lock exists here only for the responder.
            receivable(c, their_name, their_leg, Payee::Receiver, their_asset.as_ref(), role == Role::Responder)?;
            receivable(c, own_name, own_leg, Payee::RefundTo, own_asset.as_ref(), false)?;
            let fee = match (&own_asset, own_leg.asset.is_native()) {
                (Some(a), _) => a.transfer_fee,
                (None, true) => None,
                (None, false) => return Err(checks::violation(code::S8, "own asset facts unknown")),
            };
            let ctx = BindContext {
                transfer_fee: fee,
                solana_mode: env.runtime.solana_mode.clone(),
                max_fee: own_profile.fees.worst_lock,
                tip_height: bitcoin_tip(c, own_leg),
                lookup_tables: lookup_tables(c, own_leg),
                token_program: own_asset.as_ref().and_then(|a| a.token_program),
                ..Default::default()
            };
            binding = Some(s24(tx::bind(action, own_leg, need_tx(req)?, env.own, &ctx))?);
        }
        Action::Reveal => {
            let (facts, _) = observed_leg(c, LegName::B, &terms.leg_b)?;
            let method = c.weakest.unwrap_or(EvidenceMethod::SingleRpc);
            checks::s14(&facts, method, checks::band(pb, notional)?)?;
            let now_b = c.chain_now(&terms.leg_b.chain, facts.timelock);
            let Some(now_b) = now_b else {
                return Err(checks::violation(code::S12, "chain tip unknown"));
            };
            checks::s12(facts.timelock, now_b, pb, margin)?;
            let preimage = req.preimage.filter(|p| preimage_opens(p, &terms.leg_b.lock.hashlock));
            let Some(preimage) = preimage else {
                return Err(checks::violation(code::S2, "the secret does not open the hashlock"));
            };
            // S27 again: the own receiver on leg B can still receive the asset.
            receivable(c, LegName::B, &terms.leg_b, Payee::Receiver, their_asset.as_ref(), true)?;
            counterparty_lock = Some(LockObs { chain: terms.leg_b.chain.id(), depth: Some(facts.confirmations), finalized: facts.finalized });
            let mut ctx = BindContext {
                preimage: Some(preimage),
                htlc_outpoint: facts.outpoint,
                solana_mode: env.runtime.solana_mode.clone(),
                max_fee: pb.fees.worst_claim,
                tip_height: bitcoin_tip(c, &terms.leg_b),
                ..Default::default()
            };
            solana_facts(c, &terms.leg_b, &mut ctx);
            binding = Some(s24(tx::bind(action, &terms.leg_b, need_tx(req)?, env.own, &ctx))?);
            if terms.leg_a.lock.timelock.is_relative() {
                // The own leg A lock exists now: a relative T_A counts from its observed
                // confirmation (spec 7.3), never from the adapter's value. Without the
                // observation, the timeout_gap fact stays unknown.
                let tip_a = c.resolve(&format!("tip:{}", terms.leg_a.chain), env.obs.tips.get(&terms.leg_a.chain));
                ta = c
                    .resolve_at("lock:A", env.obs.locks.get(&LegName::A))
                    .and_then(|(facts, seen_at)| checks::observed_timelock(&terms.leg_a, &facts, seen_at, tip_a));
            }
        }
        Action::Claim | Action::Refund => unreachable!("exit actions take the exit path"),
    }

    // Derived clock facts through the verified arithmetic.
    let (timeout_gap, reveal_window) = match (ta, tb_terms) {
        (Some(ta), Some(tb)) => {
            let now_a = c.chain_now(&terms.leg_a.chain, ta);
            let now_b = c.chain_now(&terms.leg_b.chain, tb);
            let gap = now_a.zip(now_b).map(|(na, nb)| verified::timeout_gap(ta, na, pa.clock, tb, nb, pb.clock));
            let window = now_b.map(|nb| verified::reveal_window(tb, nb, pb.clock, pb.d_confirm_secs, margin));
            (gap, window)
        }
        (None, Some(tb)) => (None, c.chain_now(&terms.leg_b.chain, tb).map(|nb| verified::reveal_window(tb, nb, pb.clock, pb.d_confirm_secs, margin))),
        _ => (None, None),
    };

    let f3 = build_facts(env, terms, role, own_leg, their_leg, FactsIn {
        notional,
        deviation,
        timeout_gap,
        reveal_window,
        counterparty_lock,
        asset_risk: own_asset.as_ref().zip(their_asset.as_ref()).map(|(a, b)| a.risk_mask() | b.risk_mask()),
        own_profile,
        their_profile,
        evidence: c.weakest,
    });
    record_facts(c, &f3);
    let decision = verified::decide(&policy.rule, &f3);
    // A missing price never goes to the owner as Ask: the trade is denied. The
    // verified decision stays sound; this only turns Ask into Deny.
    if matches!(decision, Decision::Ask) && reads_unknown_price(&policy.rule, notional, deviation) {
        return Err(checks::violation(code::PRICE, "no valid price for a value that the policy reads"));
    }
    Ok((decision, binding))
}

/// The rule reads a price-derived fact (notional, period notional or price
/// deviation) that is unknown: a leg has no valid price or no asset facts.
fn reads_unknown_price(rule: &SwapRule, notional: Option<u64>, deviation: Option<u64>) -> bool {
    match rule {
        SwapRule::All(rules) | SwapRule::Any(rules) => rules.iter().any(|r| reads_unknown_price(r, notional, deviation)),
        SwapRule::NotionalAtMost(_) | SwapRule::PeriodNotionalAtMost(..) => notional.is_none(),
        SwapRule::PriceDeviationAtMost(_) => deviation.is_none(),
        _ => false,
    }
}

struct FactsIn<'p> {
    notional: Option<u64>,
    deviation: Option<u64>,
    timeout_gap: Option<u64>,
    reveal_window: Option<u64>,
    counterparty_lock: Option<LockObs>,
    asset_risk: Option<u32>,
    own_profile: &'p ChainProfile,
    their_profile: &'p ChainProfile,
    evidence: Option<EvidenceMethod>,
}

fn build_facts(env: &Env, terms: &Terms, role: Role, own_leg: &Leg, their_leg: &Leg, f: FactsIn) -> SwapFacts3 {
    let p = &env.policy.policy;
    let approved = |list: &[String], a: &str| list.iter().any(|x| x == a);
    let counterparty = env.obs.identity.as_ref().and_then(|cred| {
        let ok = cred.signature_ok
            && approved(&p.authorities.identity, &cred.authority)
            && cred.identity == role.counterparty(terms)
            && cred.valid_until >= env.now_real;
        ok.then(|| crate::sha256(cred.identity.as_bytes()))
    });
    let list_check = env.obs.list.as_ref().and_then(|l| {
        (l.signature_ok && approved(&p.authorities.list, &l.authority))
            .then_some(ListCheck { list_hash: l.list_hash, listed: l.contains_counterparty })
    });
    let collateral_bps = env.obs.collateral.as_ref().and_then(|r| {
        (r.signature_ok && approved(&p.authorities.collateral, &r.authority)).then_some(r.bps)
    });
    let period_spent = env
        .policy
        .periods
        .iter()
        .map(|&period| PeriodSpent {
            period,
            spent: env.ledger.period_spent_excluding(period, env.now_real, Some(&terms.swap_id)),
        })
        .collect();
    // Selecting facts come from the terms, which the signed ACCEPT message fixes.
    let contract_pinned = Some(
        env.policy.pin_for(&own_leg.chain, &own_leg.lock.contract).is_some()
            && env.policy.pin_for(&their_leg.chain, &their_leg.lock.contract).is_some(),
    );
    let _ = (f.own_profile, f.their_profile);
    SwapFacts3 {
        give_chain: own_leg.chain.id(),
        take_chain: their_leg.chain.id(),
        give_asset: own_leg.asset.id(),
        take_asset: their_leg.asset.id(),
        counterparty,
        list_check,
        notional: f.notional,
        period_spent,
        open_swaps: env.ledger.open_swaps_including(&terms.swap_id),
        evidence: f.evidence.map(EvidenceMethod::level),
        price_deviation_bps: f.deviation,
        timeout_gap: f.timeout_gap,
        reveal_window: f.reveal_window,
        counterparty_lock: f.counterparty_lock,
        asset_risk: f.asset_risk,
        contract_pinned,
        collateral_bps,
    }
}

fn record_facts(c: &mut Collected, f: &SwapFacts3) {
    let derived = || Some(Provenance::Derived { inputs: vec!["terms".into(), "observations".into()] });
    c.record("give_asset", json!(crate::to_hex(&f.give_asset)), None);
    c.record("take_asset", json!(crate::to_hex(&f.take_asset)), None);
    c.record("counterparty", opt(f.counterparty.map(|h| crate::to_hex(&h))), None);
    c.record("list_check", opt(f.list_check.map(|l| json!({"list_hash": crate::to_hex(&l.list_hash), "listed": l.listed}))), None);
    c.record("notional", opt(f.notional), derived());
    for p in &f.period_spent {
        c.record(&format!("period_spent:{}", p.period), opt(p.spent), Some(Provenance::Ledger { counter: c.env.ledger.ledger.counter }));
    }
    c.record("open_swaps", opt(f.open_swaps), Some(Provenance::Ledger { counter: c.env.ledger.ledger.counter }));
    c.record("evidence", opt(f.evidence), derived());
    c.record("price_deviation_bps", opt(f.price_deviation_bps), derived());
    c.record("timeout_gap", opt(f.timeout_gap), derived());
    c.record("reveal_window", opt(f.reveal_window), derived());
    c.record(
        "counterparty_lock",
        opt(f.counterparty_lock.map(|l| json!({"chain": crate::to_hex(&l.chain), "depth": l.depth, "finalized": l.finalized}))),
        None,
    );
    c.record("asset_risk", opt(f.asset_risk), None);
    c.record("contract_pinned", opt(f.contract_pinned), None);
    c.record("collateral_bps", opt(f.collateral_bps), None);
}

/// Claim and refund: structural checks only. The policy never blocks an exit
/// (spec 3.3, S18); neither the policy version nor the evaluator is consulted.
fn exit(req: &Request, env: &Env, c: &mut Collected) -> Result<Option<TxBinding>, Violation> {
    let (terms, role, action) = (&req.terms, req.role, req.action);
    if !action.allowed_for(role) {
        return Err(checks::violation(code::ROLE, format!("{action:?} is not an action of the {role:?}")));
    }
    checks::s1(terms)?;
    checks::s3(terms)?;
    let leg_name = action.leg(role).expect("exits touch a leg");
    let leg = terms.leg(leg_name);
    // The profile, not the policy, sets the fee limit: the policy never blocks an exit.
    let Some(fees) = env.profiles.get(&leg.chain).map(|p| &p.fees) else {
        return Err(checks::violation(code::CHAIN, format!("{} has no profile", leg.chain)));
    };
    let max_fee = if action == Action::Refund { fees.worst_refund } else { fees.worst_claim };
    let mut ctx = BindContext { solana_mode: env.runtime.solana_mode.clone(), max_fee, ..Default::default() };
    ctx.tip_height = bitcoin_tip(c, leg);
    solana_facts(c, leg, &mut ctx);
    match action {
        Action::Claim => {
            let (facts, _) = observed_leg(c, leg_name, leg)?;
            let preimage = req.preimage.filter(|p| preimage_opens(p, &leg.lock.hashlock));
            if preimage.is_none() {
                return Err(checks::violation(code::S2, "the observed preimage does not open the hashlock"));
            }
            c.record("preimage_opens_hashlock", Value::Bool(true), None);
            ctx.preimage = preimage;
            ctx.htlc_outpoint = facts.outpoint;
        }
        Action::Refund => {
            // A refund pays the fixed refund account; on Bitcoin the input is the observed output.
            if let Some(facts) = c.resolve(&format!("lock:{leg_name:?}"), env.obs.locks.get(&leg_name)) {
                ctx.htlc_outpoint = facts.outpoint;
            }
        }
        _ => unreachable!(),
    }
    let binding = s24(tx::bind(action, leg, need_tx(req)?, env.own, &ctx))?;
    Ok(Some(binding))
}
