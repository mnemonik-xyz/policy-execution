//! The authorization pipeline (implementation spec 4.7).
//!
//! Entry actions: obligatory checks → facts → verified evaluator → a warrant on
//! `Allow`, a decision record on `Deny` or `Ask`. Exit actions: structural checks
//! only; the evaluator is never called (S18); a failure halts the signer.

use crate::caip::{AssetId, ChainId};
use crate::checks::{self, code, AssetFacts, ContractObservation, LockFacts, Runtime, Violation};
use crate::dsl::CompiledPolicy;
use crate::facts::{self, EvidenceMethod, FactRecord, Observed, PriceReport, Provenance};
use crate::ledger::LedgerState;
use crate::profile::{ChainProfile, ProfileSet};
use crate::secret::preimage_opens;
use crate::tx::{self, BindContext, OwnAccounts, ProposedTx};
use crate::types::{Action, Leg, LegName, Role, Terms};
use crate::verified::{self, ChainNow, Decision, ListCheck, LockObs, PeriodSpent, SwapFacts3, Timelock};
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

/// Everything the signer observed, each with its provenance.
#[derive(Clone, Debug, Default)]
pub struct Observations {
    pub tips: BTreeMap<ChainId, Observed<u64>>,
    pub locks: BTreeMap<LegName, Observed<LockFacts>>,
    pub contracts: BTreeMap<LegName, Observed<ContractObservation>>,
    pub assets: BTreeMap<AssetId, Observed<AssetFacts>>,
    pub prices: Vec<PriceReport>,
    pub identity: Option<IdentityCredential>,
    pub list: Option<ListSnapshot>,
    pub collateral: Option<CollateralReport>,
    pub fee_reserves: BTreeMap<ChainId, u128>,
}

/// What the agent proposes. Nothing here is a fact: terms are checked against the
/// signed ACCEPT message hash and chain observations; the transaction is decoded.
#[derive(Clone, Debug)]
pub struct Request {
    pub action: Action,
    pub role: Role,
    pub terms: Terms,
    /// `blake3` of the signed ACCEPT message.
    pub inner_sig_hash: Hash32,
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
    Record(Box<DecisionRecord>),
}

impl Outcome {
    pub fn is_allow(&self) -> bool {
        matches!(self, Outcome::Warrant(_))
    }

    pub fn record_decision(&self) -> Option<RecordDecision> {
        match self {
            Outcome::Record(r) => Some(r.decision),
            Outcome::Warrant(_) => None,
        }
    }

    pub fn reasons(&self) -> &[String] {
        match self {
            Outcome::Warrant(w) => &w.reasons,
            Outcome::Record(r) => &r.reasons,
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
        let (value, prov) = obs?.resolve(self.env.policy.policy.quorum)?;
        if let Provenance::Chain { method, .. } = &prov {
            self.weakest = Some(self.weakest.map_or(*method, |w| w.min(*method)));
        }
        self.records.push(FactRecord::new(name, Value::Bool(true), Some(prov)));
        Some(value)
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
        Err(e) => return record(req, env, [0; 32], RecordDecision::Deny, vec![format!("TERMS_ENCODING: {e}")], vec![]),
    };
    if req.action.is_exit() {
        match exit(req, env, &mut c) {
            Ok(binding) => warrant(req, env, terms_hash, binding, vec!["EXIT_ACTION".into()], c.records),
            Err(v) => record(req, env, terms_hash, RecordDecision::Halt, vec![v.to_string()], c.records),
        }
    } else {
        match entry(req, env, &mut c) {
            Ok((Decision::Allow, binding)) => warrant(req, env, terms_hash, binding, vec!["POLICY_ALLOW".into()], c.records),
            Ok((Decision::Ask, _)) => record(req, env, terms_hash, RecordDecision::Ask, vec!["POLICY_ASK".into()], c.records),
            Ok((Decision::Deny, _)) => record(req, env, terms_hash, RecordDecision::Deny, vec!["POLICY_DENY".into()], c.records),
            Err(v) => record(req, env, terms_hash, RecordDecision::Deny, vec![v.to_string()], c.records),
        }
    }
}

fn warrant(req: &Request, env: &Env, terms_hash: Hash32, binding: Option<TxBinding>, reasons: Vec<String>, facts: Vec<FactRecord>) -> Outcome {
    let leg = req.action.leg(req.role).map(|l| req.terms.leg(l).clone());
    Outcome::Warrant(Box::new(SwapWarrant {
        protocol: PROTOCOL.into(),
        action: req.action,
        swap_id: req.terms.swap_id,
        terms_hash,
        inner_sig_hash: req.inner_sig_hash,
        leg,
        tx_binding: binding,
        facts,
        policy_hash: env.policy.policy_hash,
        policy_version: env.policy.policy.version,
        evaluator_id: EvaluatorId::Build(env.policy.policy.evaluator_id),
        decision: "allow".into(),
        reasons,
        valid_after: env.now_real,
        valid_until: env.now_real.saturating_add(req.valid_for_secs),
        nonce: req.nonce,
        prev_warrant: req.prev_warrant,
    }))
}

fn record(req: &Request, env: &Env, terms_hash: Hash32, decision: RecordDecision, reasons: Vec<String>, facts: Vec<FactRecord>) -> Outcome {
    Outcome::Record(Box::new(DecisionRecord {
        protocol: DECISION_PROTOCOL.into(),
        action: req.action,
        swap_id: req.terms.swap_id,
        terms_hash,
        decision,
        reasons,
        facts,
        policy_hash: env.policy.policy_hash,
        policy_version: env.policy.policy.version,
        evaluator_id: EvaluatorId::Build(env.policy.policy.evaluator_id),
        at: env.now_real,
        nonce: req.nonce,
        prev_warrant: req.prev_warrant,
    }))
}

fn need_tx(req: &Request) -> Result<&ProposedTx, Violation> {
    req.tx.as_ref().ok_or_else(|| Violation { code: code::S24, detail: "no transaction proposed".into() })
}

fn s24(r: Result<TxBinding, String>) -> Result<TxBinding, Violation> {
    r.map_err(|detail| Violation { code: code::S24, detail })
}

/// Observed lock and contract identity of one leg (S5–S10, S7).
fn observed_leg(c: &mut Collected, leg_name: LegName, leg: &Leg) -> Result<LockFacts, Violation> {
    let env = c.env;
    let facts = c
        .resolve(&format!("lock:{leg_name:?}"), env.obs.locks.get(&leg_name))
        .ok_or_else(|| Violation { code: code::S14, detail: format!("lock of leg {leg_name:?} not observed with agreeing evidence") })?;
    let expected = leg.lock.timelock.absolute(None, None);
    checks::observed_lock(leg, &facts, expected)?;
    let contract = match &facts.script_pubkey {
        Some(spk) => Some(ContractObservation::Bitcoin { script_pubkey: spk.clone() }),
        None => c.resolve(&format!("contract:{leg_name:?}"), env.obs.contracts.get(&leg_name)),
    };
    checks::s7(leg, env.policy, contract.as_ref())?;
    Ok(facts)
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
    checks::s5_s6_terms(terms, role, &env.own.accounts)?;
    checks::timelock_form(&terms.leg_a)?;
    checks::timelock_form(&terms.leg_b)?;

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
    let notional = match (whole(give_e8), whole(take_e8)) {
        (Some(g), Some(t)) => Some(g.max(t)),
        (g, t) => g.or(t),
    };
    let deviation = give_e8.zip(take_e8).and_then(|(g, t)| facts::deviation_bps(g, t));

    // Timelocks as agreed (absolute) or as observed.
    let ta_terms = terms.leg_a.lock.timelock.absolute(None, None);
    let tb_terms = terms.leg_b.lock.timelock.absolute(None, None);
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
            if let (Role::Responder, Some(ta), Some(tb)) = (role, ta_terms, tb_terms) {
                let (now_a, now_b) = (c.chain_now(&terms.leg_a.chain, ta), c.chain_now(&terms.leg_b.chain, tb));
                let (Some(now_a), Some(now_b)) = (now_a, now_b) else {
                    return Err(checks::violation(code::S11, "chain tip unknown"));
                };
                checks::s11(ta, now_a, pa, tb, now_b, pb, margin)?;
            }
        }
        Action::Lock => {
            let own_contract = c.resolve(&format!("contract:{own_name:?}"), env.obs.contracts.get(&own_name));
            checks::s7(own_leg, policy, own_contract.as_ref())?;
            checks::s15(&env.obs.fee_reserves, own_profile, their_profile)?;
            checks::s16(env.runtime)?;
            checks::s17(own_profile, env.runtime)?;
            checks::s19(own_profile)?;
            if role == Role::Responder {
                // S13: the initiator lock is final and leaves room for T_B.
                let facts = observed_leg(c, LegName::A, &terms.leg_a)
                    .map_err(|v| Violation { code: code::S13, detail: v.to_string() })?;
                let method = c.weakest.unwrap_or(EvidenceMethod::SingleRpc);
                checks::s14(&facts, method, checks::band(pa, notional)?)?;
                let Some(tb) = tb_terms else {
                    return Err(checks::violation(code::TIMELOCK, "the second leg needs an absolute timelock"));
                };
                let (now_a, now_b) = (c.chain_now(&terms.leg_a.chain, facts.timelock), c.chain_now(&terms.leg_b.chain, tb));
                let (Some(now_a), Some(now_b)) = (now_a, now_b) else {
                    return Err(checks::violation(code::S13, "chain tip unknown"));
                };
                checks::s11(facts.timelock, now_a, pa, tb, now_b, pb, margin)
                    .map_err(|v| Violation { code: code::S13, detail: v.detail })?;
                ta = Some(facts.timelock);
                counterparty_lock = Some(LockObs { chain: terms.leg_a.chain.id(), depth: Some(facts.confirmations), finalized: facts.finalized });
            }
            let fee = match (&own_asset, own_leg.asset.is_native()) {
                (Some(a), _) => a.transfer_fee,
                (None, true) => None,
                (None, false) => return Err(checks::violation(code::S8, "own asset facts unknown")),
            };
            let ctx = BindContext { transfer_fee: fee, solana_mode: env.runtime.solana_mode.clone(), ..Default::default() };
            binding = Some(s24(tx::bind(action, own_leg, need_tx(req)?, env.own, &ctx))?);
        }
        Action::Reveal => {
            let facts = observed_leg(c, LegName::B, &terms.leg_b)?;
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
            counterparty_lock = Some(LockObs { chain: terms.leg_b.chain.id(), depth: Some(facts.confirmations), finalized: facts.finalized });
            let ctx = BindContext {
                preimage: Some(preimage),
                htlc_outpoint: facts.outpoint,
                solana_mode: env.runtime.solana_mode.clone(),
                ..Default::default()
            };
            binding = Some(s24(tx::bind(action, &terms.leg_b, need_tx(req)?, env.own, &ctx))?);
        }
        Action::Claim | Action::Refund => unreachable!("exit actions take the exit path"),
    }

    // Derived clock facts through the verified arithmetic.
    let (timeout_gap, reveal_window) = match (ta, tb_terms) {
        (Some(ta), Some(tb)) => {
            let now_a = c.chain_now(&terms.leg_a.chain, ta);
            let now_b = c.chain_now(&terms.leg_b.chain, tb);
            let gap = now_a.zip(now_b).map(|(na, nb)| verified::timeout_gap(ta, na, pa.clock, tb, nb, pb.clock));
            let window = now_b.map(|nb| verified::reveal_window(tb, nb, pb.clock, pb.d_confirm_secs));
            (gap, window)
        }
        (None, Some(tb)) => (None, c.chain_now(&terms.leg_b.chain, tb).map(|nb| verified::reveal_window(tb, nb, pb.clock, pb.d_confirm_secs))),
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
    Ok((decision, binding))
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
        return Err(Violation { code: code::ROLE, detail: format!("{action:?} is not an action of the {role:?}") });
    }
    checks::s1(terms)?;
    checks::s3(terms)?;
    let leg_name = action.leg(role).expect("exits touch a leg");
    let leg = terms.leg(leg_name);
    let mut ctx = BindContext { solana_mode: env.runtime.solana_mode.clone(), ..Default::default() };
    match action {
        Action::Claim => {
            let facts = observed_leg(c, leg_name, leg)?;
            let preimage = req.preimage.filter(|p| preimage_opens(p, &leg.lock.hashlock));
            if preimage.is_none() {
                return Err(Violation { code: code::S2, detail: "the observed preimage does not open the hashlock".into() });
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
