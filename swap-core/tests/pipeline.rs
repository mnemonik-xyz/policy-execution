//! End-to-end tests of the authorization pipeline: a BTC (Bitcoin, leg A) for USDC
//! (Ethereum, leg B) swap, from both sides. Each obligatory check has a test in
//! which only that check fails; the fault-injection tests of spec 13.3 that do
//! not need a running signer are here too (1, 2, 3, 5, 6, 7, 8, 10). A BTC for SOL
//! variant covers the Solana escrow account in S7.

use bitcoin as rb;
use rb::hashes::Hash as _;
use warrant_swap_core::authorize::{authorize, AcceptEvidence, Env, IdentityCredential, Observations, Outcome, Request};
use warrant_swap_core::caip::{AccountId, AssetId, ChainId};
use warrant_swap_core::checks::{code, AssetFacts, ContractObservation, LockFacts, Payee, ReceiverFacts, Runtime};
use warrant_swap_core::dsl::{validate_policy, CompiledPolicy, ContractPinSpec};
use warrant_swap_core::evm::{self, ContractFacts, Eip1559Tx, LockCall};
use warrant_swap_core::facts::{EvidenceMethod, Observed, PriceReport, Report, TransferFee};
use warrant_swap_core::ledger::LedgerState;
use warrant_swap_core::profile::{reference, ProfileSet};
use warrant_swap_core::tx::{OwnAccounts, ProposedTx};
use warrant_swap_core::types::{Action, HashAlg, Leg, LegName, Lock, RiskFlag, Role, Terms, TimelockSpec};
use warrant_swap_core::verified::Timelock;
use warrant_swap_core::warrant::{RecordDecision, TxBinding};
use warrant_swap_core::{bitcoin as btc, solana as sol, Hash32};

const NOW: u64 = 1_800_000_000;
const TIP: u64 = 900_000;
const EVM_TIP: u64 = 20_000_000;
const T_A: u64 = TIP + 288;
const T_B: u64 = NOW + 6 * 3_600;
const BTC_CHAIN: &str = reference::BITCOIN_MAINNET;
const ETH_CHAIN: &str = reference::ETHEREUM_MAINNET;
const BTC: &str = "bip122:000000000019d6689c085ae165831e93/slip44:0";
const USDC: &str = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48";
const HTLC_EVM: &str = "0x3333333333333333333333333333333333333333";
const SECRET: Hash32 = [0x5e; 32];
const SWAP_ID: Hash32 = [0x51; 32];
const BUILD: Hash32 = [0xee; 32];
const RESPONDER: &str = "did:key:z6MkResponder";
const INITIATOR: &str = "did:key:z6MkInitiator";

fn sha256(b: &[u8]) -> Hash32 {
    warrant_swap_core::sha256(b)
}

fn xonly(seed: u8) -> Hash32 {
    let secp = rb::secp256k1::Secp256k1::new();
    let sk = rb::secp256k1::SecretKey::from_slice(&[seed; 32]).unwrap();
    rb::secp256k1::Keypair::from_secret_key(&secp, &sk).x_only_public_key().0.serialize()
}

fn p2tr(seed: u8) -> Vec<u8> {
    let mut s = vec![0x51, 32];
    s.extend(xonly(seed));
    s
}

fn acct(text: &str) -> AccountId {
    AccountId::parse(text).unwrap()
}

fn eth(byte: &str) -> AccountId {
    acct(&format!("{ETH_CHAIN}:0x{}", byte.repeat(20)))
}

/// Each lock carries `sha256(swap_id ‖ leg ‖ sender)` (spec 3.2), as honest terms do.
fn with_lock_ids(mut t: Terms) -> Terms {
    for which in [LegName::A, LegName::B] {
        let id = t.leg(which).derived_lock_id(which).expect("sender bytes");
        match which {
            LegName::A => t.leg_a.lock.lock_id = id,
            LegName::B => t.leg_b.lock.lock_id = id,
        }
    }
    t
}

fn terms() -> Terms {
    let h = sha256(&SECRET);
    with_lock_ids(Terms {
        swap_id: SWAP_ID,
        initiator: INITIATOR.into(),
        responder: RESPONDER.into(),
        leg_a: Leg {
            chain: ChainId::parse(BTC_CHAIN).unwrap(),
            asset: AssetId::parse(BTC).unwrap(),
            amount: 50_000_000,
            sender: acct(&format!("{BTC_CHAIN}:bc1pinitiator")),
            receiver: acct(&format!("{BTC_CHAIN}:bc1presponder")),
            refund_to: acct(&format!("{BTC_CHAIN}:bc1pinitiator")),
            lock: Lock {
                contract: btc::TEMPLATE_ID.into(),
                hash_alg: HashAlg::Sha256,
                hashlock: h,
                preimage_len: 32,
                timelock: TimelockSpec::Height(T_A),
                swap_id: SWAP_ID,
                lock_id: [0; 32],
                claim_key: Some(xonly(20)),
                refund_key: Some(xonly(10)),
            },
        },
        leg_b: Leg {
            chain: ChainId::parse(ETH_CHAIN).unwrap(),
            asset: AssetId::parse(USDC).unwrap(),
            amount: 30_000_000_000,
            sender: eth("bb"),
            receiver: eth("aa"),
            refund_to: eth("bb"),
            lock: Lock {
                contract: HTLC_EVM.into(),
                hash_alg: HashAlg::Sha256,
                hashlock: h,
                preimage_len: 32,
                timelock: TimelockSpec::Time(T_B),
                swap_id: SWAP_ID,
                lock_id: [0; 32],
                claim_key: None,
                refund_key: None,
            },
        },
    })
}

fn profiles() -> ProfileSet {
    ProfileSet::new(vec![reference::bitcoin(BTC_CHAIN), reference::ethereum()])
}

fn rule() -> serde_json::Value {
    serde_json::json!({ "all": [
        { "pair_in": [[BTC, USDC], [USDC, BTC]] },
        { "notional_at_most": ["USD", 50000] },
        { "period_notional_at_most": ["P1D", 200000] },
        { "price_deviation_at_most": 50 },
        { "timeout_gap_at_least": 7200 },
        { "finality_at_least": [BTC_CHAIN, 3] },
        { "evidence_at_least": "light_client" },
        { "asset_risk_within": ["freezable_by_issuer"] },
        { "any": [ { "counterparty_in": [RESPONDER, INITIATOR] },
                   { "notional_at_most": ["USD", 1000] } ] }
    ]})
}

fn policy_with(rule: serde_json::Value, version: u64) -> CompiledPolicy {
    let p = profiles();
    let b = p.get(&ChainId::parse(BTC_CHAIN).unwrap()).unwrap();
    let e = p.get(&ChainId::parse(ETH_CHAIN).unwrap()).unwrap();
    let doc = serde_json::json!({
        "version": version,
        "ref_ccy": "USD",
        "evaluator_id": warrant_swap_core::to_hex(&BUILD),
        "chains": {
            (BTC_CHAIN): { "profile_hash": warrant_swap_core::to_hex(&b.hash()), "contracts": [{"bitcoin_template": btc::TEMPLATE_ID}] },
            (ETH_CHAIN): { "profile_hash": warrant_swap_core::to_hex(&e.hash()), "contracts": [{"evm": {"address": HTLC_EVM, "code_hash": warrant_swap_core::to_hex(&[0xc0; 32])}}] }
        },
        "authorities": { "oracle": ["pyth"], "identity": ["mnemonik"] },
        "oracle": { "max_age_secs": 60, "max_conf_bps": 50 },
        "quorum": 2,
        "margin_secs": 1800,
        "rule": rule
    });
    validate_policy(&doc.to_string(), &p).unwrap()
}

/// One own-node report at `height`.
fn chain_obs_at<T: Clone + PartialEq>(height: u64, value: T) -> Observed<T> {
    Observed::single(EvidenceMethod::OwnNode, "own", [0xb2; 32], height, value)
}

fn chain_obs<T: Clone + PartialEq>(method: EvidenceMethod, value: T) -> Observed<T> {
    match method {
        EvidenceMethod::RpcQuorum => Observed {
            method,
            reports: ["rpc-1", "rpc-2"]
                .iter()
                .map(|p| Report { provider: p.to_string(), block_hash: [0xb1; 32], height: 1, value: value.clone() })
                .collect(),
        },
        _ => Observed::single(method, "own", [0xb1; 32], 1, value),
    }
}

fn lock_a_facts(confirmations: u64) -> LockFacts {
    let t = terms();
    LockFacts {
        contract: btc::TEMPLATE_ID.into(),
        lock_id: t.leg_a.lock.lock_id,
        hash_alg: HashAlg::Sha256,
        hashlock: t.leg_a.lock.hashlock,
        preimage_len_enforced: true,
        // First block in which the CLTV refund is valid.
        timelock: Timelock::Height(T_A + 1),
        receiver: t.leg_a.receiver.clone(),
        refund_to: t.leg_a.refund_to.clone(),
        asset: t.leg_a.asset.clone(),
        net_amount: t.leg_a.amount,
        confirmations,
        finalized: None,
        outpoint: Some(([0x77; 32], 0)),
        script_pubkey: Some(btc::htlc_script_pubkey(&t.leg_a.lock).unwrap()),
    }
}

fn lock_b_facts() -> LockFacts {
    let t = terms();
    LockFacts {
        contract: HTLC_EVM.into(),
        lock_id: t.leg_b.lock.lock_id,
        hash_alg: HashAlg::Sha256,
        hashlock: t.leg_b.lock.hashlock,
        preimage_len_enforced: true,
        timelock: Timelock::Time(T_B),
        receiver: t.leg_b.receiver.clone(),
        refund_to: t.leg_b.refund_to.clone(),
        asset: t.leg_b.asset.clone(),
        net_amount: t.leg_b.amount,
        confirmations: 70,
        finalized: Some(true),
        outpoint: None,
        script_pubkey: None,
    }
}

fn price(asset: &str, price_e8: u128) -> PriceReport {
    PriceReport {
        asset: AssetId::parse(asset).unwrap(),
        currency: "USD".into(),
        price_e8,
        conf_e8: 0,
        publish_time: NOW - 5,
        authority: "pyth".into(),
        signature_ok: true,
    }
}

fn usdc_receiver(payee: [u8; 20]) -> ReceiverFacts {
    ReceiverFacts::Evm {
        token: AssetId::parse(USDC).unwrap().erc20_address().unwrap(),
        payee,
        htlc: warrant_swap_core::from_hex_array(HTLC_EVM).unwrap(),
        payee_blocked: false,
        htlc_blocked: false,
        paused: false,
    }
}

fn htlc_contract() -> ContractObservation {
    ContractObservation::Evm(ContractFacts { code_hash: [0xc0; 32], slots: no_proxy_slots(), loupe: evm::Loupe::Reverted, implementation_code: None })
}

/// A storage word that holds the address `[a; 20]`.
fn address_word(a: u8) -> Hash32 {
    let mut w = [0u8; 32];
    w[12..].copy_from_slice(&[a; 20]);
    w
}

fn no_proxy_slots() -> evm::ProxySlots {
    evm::ProxySlots { eip1967_implementation: [0; 32], eip1967_admin: [0; 32], eip1967_beacon: [0; 32], zos_implementation: [0; 32], zos_admin: [0; 32], eip1822_proxiable: [0; 32] }
}

struct World {
    policy: CompiledPolicy,
    profiles: ProfileSet,
    obs: Observations,
    ledger: LedgerState,
    runtime: Runtime,
    own: OwnAccounts,
    build: Hash32,
    now: u64,
    role: Role,
    /// `None`: a verified ACCEPT of exactly the terms that `run` gets.
    accept_override: Option<Option<AcceptEvidence>>,
}

const ACCEPT_HASH: Hash32 = [0xac; 32];

fn accept_of(terms: &Terms) -> AcceptEvidence {
    AcceptEvidence { inner_sig_hash: ACCEPT_HASH, terms_hash: terms.hash().unwrap(), signature_ok: true }
}

impl World {
    fn new(role: Role) -> Self {
        let t = terms();
        let mut obs = Observations::default();
        obs.tips.insert(t.leg_a.chain.clone(), chain_obs(EvidenceMethod::LightClient, TIP));
        obs.tips.insert(t.leg_b.chain.clone(), chain_obs(EvidenceMethod::LightClient, EVM_TIP));
        obs.assets.insert(t.leg_a.asset.clone(), chain_obs(EvidenceMethod::OwnNode, AssetFacts { decimals: 8, risk_flags: vec![], transfer_fee: None, token_program: None }));
        obs.assets.insert(
            t.leg_b.asset.clone(),
            chain_obs(EvidenceMethod::OwnNode, AssetFacts { decimals: 6, risk_flags: vec![RiskFlag::FreezableByIssuer], transfer_fee: None, token_program: None }),
        );
        obs.prices = vec![price(BTC, 60_000_00000000), price(USDC, 1_00000000)];
        let counterparty = match role {
            Role::Initiator => RESPONDER,
            Role::Responder => INITIATOR,
        };
        obs.identity = Some(IdentityCredential { identity: counterparty.into(), authority: "mnemonik".into(), signature_ok: true, valid_until: NOW + 3_600 });
        obs.fee_reserves.insert(t.leg_a.chain.clone(), 1_000_000);
        obs.fee_reserves.insert(t.leg_b.chain.clone(), 10u128.pow(18));
        obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, htlc_contract()));
        // S27: both own payees of the USDC leg can receive (neither blocked, not
        // paused), read at the observed tip of leg B.
        for (payee, account) in [(Payee::Receiver, &t.leg_b.receiver), (Payee::RefundTo, &t.leg_b.refund_to)] {
            obs.receivers.insert((LegName::B, payee), chain_obs_at(EVM_TIP, usdc_receiver(account.evm_address().unwrap())));
        }
        let own = match role {
            Role::Initiator => OwnAccounts {
                accounts: vec![t.leg_a.refund_to.clone(), t.leg_b.receiver.clone()],
                bitcoin_scripts: vec![p2tr(10)],
                bitcoin_keys: vec![xonly(10)],
                solana_fee_payer: None,
            },
            Role::Responder => OwnAccounts {
                accounts: vec![t.leg_b.refund_to.clone(), t.leg_a.receiver.clone()],
                bitcoin_scripts: vec![p2tr(20)],
                bitcoin_keys: vec![xonly(20)],
                solana_fee_payer: None,
            },
        };
        World {
            policy: policy_with(rule(), 3),
            profiles: profiles(),
            obs,
            ledger: LedgerState::default(),
            runtime: Runtime { watchers_armed: true, prepared_refund: true, ..Default::default() },
            own,
            build: BUILD,
            now: NOW,
            role,
            accept_override: None,
        }
    }

    fn run(&self, action: Action, terms: Terms, tx: Option<ProposedTx>, preimage: Option<Hash32>) -> Outcome {
        let mut obs = self.obs.clone();
        obs.accept = self.accept_override.clone().unwrap_or_else(|| Some(accept_of(&terms)));
        let env = Env {
            policy: &self.policy,
            profiles: &self.profiles,
            obs: &obs,
            ledger: &self.ledger,
            runtime: &self.runtime,
            own: &self.own,
            now_real: self.now,
            evaluator_build: self.build,
        };
        let req = Request {
            action,
            role: self.role,
            terms,
            tx,
            preimage,
            nonce: [0x0f; 16],
            prev_warrant: None,
            valid_for_secs: 600,
        };
        authorize(&req, &env)
    }
}

fn reason(o: &Outcome) -> String {
    o.reasons().join(";")
}

/// The reasons and the local diagnostic, for failure messages.
fn explain(o: &Outcome) -> String {
    format!("{} ({:?})", reason(o), o.diagnostic())
}

/// Spec 4.1 (D7): every reason is one of the fixed codes, never a detail.
fn assert_fixed_codes(o: &Outcome) {
    for r in o.reasons() {
        assert!(code::ALL.contains(&r.as_str()), "reason {r:?} is not a fixed code");
    }
}

fn assert_allow(o: &Outcome) {
    assert!(o.is_allow(), "expected Allow, got {:?}: {}", o.record_decision(), explain(o));
    let expected = match o {
        Outcome::Warrant(w) if w.action.is_exit() => code::EXIT,
        _ => code::ALLOW,
    };
    assert_eq!(o.reasons(), [expected]);
}

/// The first reason is `code`. A second reason is the inner check that `code` wraps.
fn assert_denied(o: &Outcome, code: &str) {
    assert_eq!(o.record_decision(), Some(RecordDecision::Deny), "{}", explain(o));
    assert_fixed_codes(o);
    assert_eq!(o.reasons().first().map(String::as_str), Some(code), "{}", explain(o));
}

fn assert_halt(o: &Outcome, code: &str) {
    assert_eq!(o.record_decision(), Some(RecordDecision::Halt), "{}", explain(o));
    assert_fixed_codes(o);
    assert_eq!(o.reasons().first().map(String::as_str), Some(code), "{}", explain(o));
}

// ---------------------------------------------------------------------------
// Transactions as an honest agent would propose them
// ---------------------------------------------------------------------------

fn psbt(inputs: &[(Hash32, u32, u64, Vec<u8>)], outputs: &[(u64, Vec<u8>)], lock_time: u32, seq: u32) -> ProposedTx {
    let tx = rb::Transaction {
        version: rb::transaction::Version::TWO,
        lock_time: rb::absolute::LockTime::from_consensus(lock_time),
        input: inputs
            .iter()
            .map(|(txid, vout, _, _)| rb::TxIn {
                previous_output: rb::OutPoint { txid: rb::Txid::from_byte_array(*txid), vout: *vout },
                script_sig: rb::ScriptBuf::new(),
                sequence: rb::Sequence(seq),
                witness: rb::Witness::new(),
            })
            .collect(),
        output: outputs
            .iter()
            .map(|(v, s)| rb::TxOut { value: rb::Amount::from_sat(*v), script_pubkey: rb::ScriptBuf::from_bytes(s.clone()) })
            .collect(),
    };
    let mut p = rb::Psbt::from_unsigned_tx(tx).unwrap();
    for (i, (_, _, value, spk)) in inputs.iter().enumerate() {
        p.inputs[i].witness_utxo = Some(rb::TxOut { value: rb::Amount::from_sat(*value), script_pubkey: rb::ScriptBuf::from_bytes(spk.clone()) });
    }
    ProposedTx::BitcoinPsbt(p.serialize())
}

fn initiator_lock_psbt(extra_output: bool) -> ProposedTx {
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let mut outs = vec![(50_000_000, htlc), (9_990_000, p2tr(10))];
    if extra_output {
        outs.push((1_000, p2tr(99)));
    }
    psbt(&[([1; 32], 0, 60_000_000, p2tr(10))], &outs, 0, 0xffff_fffd)
}

fn evm_tx(to: &str, value: u128, data: Vec<u8>) -> Vec<u8> {
    Eip1559Tx {
        chain_id: 1,
        nonce: 0,
        max_priority_fee_per_gas: 1,
        max_fee_per_gas: 2,
        gas_limit: 300_000,
        to: Some(warrant_swap_core::from_hex_array(to).unwrap()),
        value,
        data,
        access_list_len: 0,
    }
    .encode_unsigned()
}

fn responder_lock_txs(approve: u128, lock_amount: u128) -> ProposedTx {
    let t = terms();
    let htlc: [u8; 20] = warrant_swap_core::from_hex_array(HTLC_EVM).unwrap();
    let token = t.leg_b.asset.erc20_address().unwrap();
    let call = LockCall {
        swap_id: SWAP_ID,
        leg: LegName::B,
        receiver: t.leg_b.receiver.evm_address().unwrap(),
        refund_to: t.leg_b.refund_to.evm_address().unwrap(),
        token,
        amount: lock_amount,
        hashlock: t.leg_b.lock.hashlock,
        timelock: T_B,
    };
    let token_hex = warrant_swap_core::to_hex(&token);
    ProposedTx::Evm(vec![evm_tx(&token_hex, 0, evm::approve_calldata(&htlc, approve)), evm_tx(HTLC_EVM, 0, call.calldata())])
}

fn reveal_tx(preimage: &Hash32) -> ProposedTx {
    ProposedTx::Evm(vec![evm_tx(HTLC_EVM, 0, evm::claim_calldata(&terms().leg_b.lock.lock_id, preimage))])
}

// ---------------------------------------------------------------------------
// The initiator
// ---------------------------------------------------------------------------

fn initiator_reveal_world() -> World {
    let mut w = World::new(Role::Initiator);
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, lock_b_facts()));
    w
}

#[test]
fn initiator_happy_path() {
    let w = World::new(Role::Initiator);
    let accept = w.run(Action::Accept, terms(), None, None);
    assert_allow(&accept);
    let Outcome::Warrant(aw) = &accept else { unreachable!() };
    assert!(aw.leg.is_none() && aw.tx_binding.is_none());
    assert!(aw.facts.iter().any(|f| f.name == "notional" && f.value == serde_json::json!(30_000)));

    let lock = w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None);
    assert_allow(&lock);
    let Outcome::Warrant(lw) = &lock else { unreachable!() };
    assert!(matches!(&lw.tx_binding, Some(TxBinding::Bitcoin { sighashes, .. }) if sighashes.len() == 1));
    assert_eq!(lw.leg.as_ref().unwrap().chain.as_str(), BTC_CHAIN);

    let w = initiator_reveal_world();
    let reveal = w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET));
    assert_allow(&reveal);
    let Outcome::Warrant(rw) = &reveal else { unreachable!() };
    assert!(matches!(&rw.tx_binding, Some(TxBinding::Evm { signing_hashes }) if signing_hashes.len() == 1));
    // The payload is canonical and the hash chain can link to it.
    assert_eq!(rw.hash().unwrap(), warrant_swap_core::blake3(&rw.payload().unwrap()));
}

#[test]
fn fault_1_long_preimage_is_rejected() {
    let w = World::new(Role::Initiator);
    let mut t = terms();
    t.leg_b.lock.preimage_len = 33;
    assert_denied(&w.run(Action::Accept, t, None, None), code::S2);
    // An observed lock that does not enforce the length is rejected too.
    let mut w = initiator_reveal_world();
    let mut f = lock_b_facts();
    f.preimage_len_enforced = false;
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, f));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S2);
}

#[test]
fn fault_2_reveal_after_deadline() {
    let mut w = initiator_reveal_world();
    w.obs.prices = vec![
        PriceReport { publish_time: T_B - 3_620, ..price(BTC, 60_000_00000000) },
        PriceReport { publish_time: T_B - 3_620, ..price(USDC, 1_00000000) },
    ];
    w.obs.identity.as_mut().unwrap().valid_until = T_B;
    // d_confirm(B) = 30 min, margin = 30 min, lead = 15 s: deadline is T_B − 3,615 s.
    w.now = T_B - 3_615;
    assert_allow(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)));
    w.now = T_B - 3_614;
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S12);
}

#[test]
fn fault_3_reorg_removes_the_counterparty_lock() {
    let mut w = initiator_reveal_world();
    w.obs.locks.remove(&LegName::B);
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S14);
    // Not yet final for the value band.
    let mut w = initiator_reveal_world();
    let mut f = lock_b_facts();
    f.finalized = Some(false);
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, f));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S14);
}

#[test]
fn fault_5_rpc_providers_disagree() {
    let mut w = initiator_reveal_world();
    let mut other = lock_b_facts();
    other.net_amount -= 1;
    let obs = Observed {
        method: EvidenceMethod::RpcQuorum,
        reports: vec![
            Report { provider: "a".into(), block_hash: [1; 32], height: 1, value: lock_b_facts() },
            Report { provider: "b".into(), block_hash: [1; 32], height: 1, value: other },
        ],
    };
    w.obs.locks.insert(LegName::B, obs);
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S14);
}

#[test]
fn fault_6_fake_token_proxy_and_permanent_delegate() {
    // A token with the same symbol at another address: the observed asset differs.
    let mut w = initiator_reveal_world();
    let mut f = lock_b_facts();
    f.asset = AssetId::parse("eip155:1/erc20:0xdead000000000000000000000000000000000000").unwrap();
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, f));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S9);
    // A proxy HTLC of each pattern: EIP-1967, beacon, legacy slots, diamond.
    type Mutation = fn(&mut ContractFacts);
    let patterns: [(&str, Mutation); 5] = [
        ("eip-1967", |f| {
            f.slots.eip1967_implementation = address_word(1);
            f.slots.eip1967_admin = address_word(2);
            f.implementation_code = Some(([1; 20], [0xc1; 32]));
        }),
        ("beacon", |f| f.slots.eip1967_beacon = address_word(3)),
        ("zeppelinos", |f| f.slots.zos_implementation = address_word(1)),
        ("eip-1822", |f| f.slots.eip1822_proxiable = address_word(1)),
        ("diamond", |f| f.loupe = evm::Loupe::Returned),
    ];
    for (name, mutate) in &patterns {
        let mut w = initiator_reveal_world();
        let ContractObservation::Evm(mut facts) = htlc_contract() else { unreachable!() };
        mutate(&mut facts);
        w.obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, ContractObservation::Evm(facts)));
        let o = w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET));
        assert_eq!(o.reasons(), [code::S7], "{name}");
        assert_denied(&o, code::S7);
    }
    // An EIP-1967 proxy HTLC passes S7 when the policy pins its admin, implementation
    // and implementation code. A beacon beside it fails S7 with the same pin.
    let mut w = initiator_reveal_world();
    let eth = ChainId::parse(ETH_CHAIN).unwrap();
    let ContractPinSpec::Evm(pin) = &mut w.policy.policy.chains.get_mut(&eth).unwrap().contracts[0] else { unreachable!() };
    pin.proxy = Some(evm::ProxyPin { admin: warrant_swap_core::to_hex(&[2; 20]), implementation: warrant_swap_core::to_hex(&[1; 20]), implementation_code_hash: [0xc1; 32] });
    let ContractObservation::Evm(mut facts) = htlc_contract() else { unreachable!() };
    (patterns[0].1)(&mut facts);
    w.obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, ContractObservation::Evm(facts.clone())));
    assert_allow(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)));
    facts.slots.eip1967_beacon = address_word(3);
    w.obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, ContractObservation::Evm(facts)));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S7);
    // An asset whose issuer can seize it is outside the allowed flags: policy Deny.
    let mut w = World::new(Role::Initiator);
    let seizable = AssetFacts { decimals: 6, risk_flags: vec![RiskFlag::SeizableByIssuer], transfer_fee: None, token_program: None };
    w.obs.assets.insert(AssetId::parse(USDC).unwrap(), chain_obs(EvidenceMethod::OwnNode, seizable));
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::DENY);
    // Flags that always deny.
    let confidential = AssetFacts { decimals: 6, risk_flags: vec![RiskFlag::ConfidentialAmount], transfer_fee: None, token_program: None };
    w.obs.assets.insert(AssetId::parse(USDC).unwrap(), chain_obs(EvidenceMethod::OwnNode, confidential));
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S8_FLAG);
}

#[test]
fn fault_7_extra_output_or_wrong_call() {
    let w = World::new(Role::Initiator);
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(true)), None), code::S24);
    let w = initiator_reveal_world();
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&[0; 32])), Some(SECRET)), code::S24);
    assert_denied(&w.run(Action::Reveal, terms(), None, Some(SECRET)), code::S24);
}

#[test]
fn bitcoin_fees_stay_within_the_profile() {
    let w = World::new(Role::Initiator);
    // A 60 000 000 sat coin into the 50 000 000 sat lock without change burns 10 000 000 sat.
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let burn = psbt(&[([1; 32], 0, 60_000_000, p2tr(10))], &[(50_000_000, htlc.clone())], 0, 0xffff_fffd);
    assert_denied(&w.run(Action::Lock, terms(), Some(burn), None), code::S24);
    // A claim that leaves the HTLC amount to the miners halts.
    let w = responder_lock_world(3);
    let claim = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(1_000, p2tr(20))], 0, 0xffff_fffd);
    assert_halt(&w.run(Action::Claim, terms(), Some(claim), Some(SECRET)), code::S24);
}

#[test]
fn fault_8_policy_rollback_and_evaluator_substitution() {
    let mut w = World::new(Role::Initiator);
    w.ledger.accept_policy(4, [0x44; 32]).unwrap();
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S22);
    let mut w = World::new(Role::Initiator);
    w.build = [0xef; 32];
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S23);
}

#[test]
fn fault_8_same_policy_version_with_another_text() {
    // The signer accepted version 3 of the owner's policy.
    let mut w = World::new(Role::Initiator);
    w.ledger.accept_policy(w.policy.policy.version, w.policy.policy_hash).unwrap();
    assert_allow(&w.run(Action::Accept, terms(), None, None));
    // A weaker policy text that also says version 3 is a different policy.
    let mut weaker = rule();
    weaker["all"][1] = serde_json::json!({ "notional_at_most": ["USD", 100000] });
    let weaker = policy_with(weaker, 3);
    assert_ne!(weaker.policy_hash, w.policy.policy_hash);
    w.policy = weaker;
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S22);
    // A higher version replaces the policy.
    w.policy = policy_with(rule(), 4);
    assert_allow(&w.run(Action::Accept, terms(), None, None));
}

#[test]
fn fault_10_initiator_outage_refunds_regardless_of_policy() {
    // The initiator missed the reveal deadline; reveal is denied …
    let mut w = initiator_reveal_world();
    w.now = T_B;
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S12);
    // … and the refund of leg A after T_A is never blocked by policy, even with a
    // rolled-back policy and a different evaluator build (S18).
    w.ledger.accept_policy(9, [0x99; 32]).unwrap();
    w.build = [0xef; 32];
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, lock_a_facts(300)));
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let refund = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(49_990_000, p2tr(10))], T_A as u32, 0xffff_fffd);
    let out = w.run(Action::Refund, terms(), Some(refund), None);
    assert_allow(&out);
    assert_eq!(out.reasons(), [code::EXIT]);
    // A refund that pays someone else halts instead.
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let theft = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(49_990_000, p2tr(99))], T_A as u32, 0xffff_fffd);
    assert_halt(&w.run(Action::Refund, terms(), Some(theft), None), code::S24);
}

#[test]
fn unknown_prices_deny() {
    // The policy reads the notional and the deviation, so a missing price denies;
    // it never goes to the owner as Ask.
    let mut w = World::new(Role::Initiator);
    w.obs.prices.clear();
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::PRICE);
    // A stale price is unknown as well.
    w.obs.prices = vec![PriceReport { publish_time: NOW - 120, ..price(BTC, 60_000_00000000) }, price(USDC, 1_00000000)];
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::PRICE);
}

#[test]
fn a_missing_price_never_reaches_the_owner() {
    let mut w = World::new(Role::Initiator);
    w.obs.prices.clear();
    // A known counterparty satisfies the rule whatever the price is: Allow stays.
    w.policy = policy_with(serde_json::json!({ "any": [
        { "counterparty_in": [RESPONDER] },
        { "notional_at_most": ["USD", 1000] }
    ]}), 3);
    assert_allow(&w.run(Action::Accept, terms(), None, None));
    // The deviation atom reads both prices as well.
    w.policy = policy_with(serde_json::json!({ "price_deviation_at_most": 50 }), 3);
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::PRICE);
    // A rule without price atoms keeps Ask for its own unknown facts.
    w.policy = policy_with(serde_json::json!({ "counterparty_in": [RESPONDER] }), 3);
    w.obs.identity = None;
    let out = w.run(Action::Accept, terms(), None, None);
    assert_eq!(out.record_decision(), Some(RecordDecision::Ask), "{}", reason(&out));
    assert_eq!(out.reasons(), [code::ASK]);
}

#[test]
fn bad_price_denies() {
    let mut w = World::new(Role::Initiator);
    w.obs.prices = vec![price(BTC, 61_000_00000000), price(USDC, 1_00000000)];
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::DENY);
}

#[test]
fn one_missing_price_denies() {
    let mut w = World::new(Role::Initiator);
    // No deviation atom, so only the notional can stop a mispriced trade.
    w.policy = policy_with(serde_json::json!({ "all": [
        { "pair_in": [[BTC, USDC], [USDC, BTC]] },
        { "notional_at_most": ["USD", 50000] }
    ]}), 3);
    assert_allow(&w.run(Action::Accept, terms(), None, None));
    // Without the BTC price, the BTC leg can be the larger one: the notional is
    // unknown, and the trade is denied.
    w.obs.prices = vec![price(USDC, 1_00000000)];
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::PRICE);
}

#[test]
fn warrant_needs_a_verified_accept_of_the_terms() {
    let mut w = World::new(Role::Initiator);
    let out = w.run(Action::Accept, terms(), None, None);
    let Outcome::Warrant(aw) = &out else { panic!("expected Allow: {}", reason(&out)) };
    assert_eq!(aw.inner_sig_hash, ACCEPT_HASH);

    // The agent proposes other terms than the counterparty signed.
    let mut changed = terms();
    changed.leg_b.amount -= 1;
    w.accept_override = Some(Some(accept_of(&terms())));
    assert_denied(&w.run(Action::Accept, changed.clone(), None, None), code::ACCEPT);
    assert_denied(&w.run(Action::Lock, changed, Some(initiator_lock_psbt(false)), None), code::ACCEPT);
    // No verified ACCEPT, or one whose signature fails.
    w.accept_override = Some(None);
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::ACCEPT);
    w.accept_override = Some(Some(AcceptEvidence { signature_ok: false, ..accept_of(&terms()) }));
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::ACCEPT);

    // An exit for terms without a verified ACCEPT halts.
    let mut w = responder_lock_world(3);
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let claim = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(49_990_000, p2tr(20))], 0, 0xffff_fffd);
    w.accept_override = Some(None);
    assert_halt(&w.run(Action::Claim, terms(), Some(claim), Some(SECRET)), code::ACCEPT);
}

#[test]
fn bitcoin_leaf_keys_are_own_keys() {
    // Leg A names the responder's address, but its claim key is the initiator's: the
    // initiator could claim both legs. The responder must not accept or lock.
    let mut stolen = terms();
    stolen.leg_a.lock.claim_key = Some(xonly(10));
    let w = responder_lock_world(3);
    assert_denied(&w.run(Action::Accept, stolen.clone(), None, None), code::S5);
    assert_denied(&w.run(Action::Lock, stolen, Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S5);
    // The initiator's own refund key must be its own, or it cannot refund leg A.
    let mut lost = terms();
    lost.leg_a.lock.refund_key = Some(xonly(20));
    let lost = with_lock_ids(lost);
    let w = World::new(Role::Initiator);
    assert_denied(&w.run(Action::Accept, lost.clone(), None, None), code::S6);
    assert_denied(&w.run(Action::Lock, lost, Some(initiator_lock_psbt(false)), None), code::S6);
    // A Bitcoin leg without leaf keys has no claim key and no refund key.
    let mut keyless = terms();
    (keyless.leg_a.lock.claim_key, keyless.leg_a.lock.refund_key) = (None, None);
    assert_denied(&responder_lock_world(3).run(Action::Accept, keyless.clone(), None, None), code::S5);
    assert_denied(&World::new(Role::Initiator).run(Action::Accept, keyless, None, None), code::S6);
}

#[test]
fn an_observed_bitcoin_lock_needs_its_output_script() {
    // Only the output script proves H, both leaf keys and T on Bitcoin. An observation
    // without it, even with a separate contract observation, proves nothing.
    let mut w = responder_lock_world(3);
    let mut facts = lock_a_facts(3);
    facts.script_pubkey = None;
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, facts.clone()));
    // The responder waits for the initiator's lock (S13), which fails S7 here.
    let lock = Some(responder_lock_txs(30_000_000_000, 30_000_000_000));
    let denied_by_s7 = |o: &Outcome| {
        assert_denied(o, code::S13);
        assert_eq!(o.reasons(), [code::S13, code::S7], "the record names the inner check");
    };
    denied_by_s7(&w.run(Action::Lock, terms(), lock.clone(), None));
    let script = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    w.obs.contracts.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, ContractObservation::Bitcoin { script_pubkey: script.clone() }));
    denied_by_s7(&w.run(Action::Lock, terms(), lock, None));
    // The responder's claim of leg A halts as well.
    let claim = psbt(&[([0x77; 32], 0, 50_000_000, script)], &[(49_990_000, p2tr(20))], 0, 0xffff_fffd);
    assert_halt(&w.run(Action::Claim, terms(), Some(claim), Some(SECRET)), code::S7);
}

#[test]
fn a_claim_may_carry_the_tip_as_nlocktime() {
    // Wallets set nLockTime to the tip against fee sniping; such a claim is final now.
    let w = responder_lock_world(3);
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let claim = |lock_time: u32| psbt(&[([0x77; 32], 0, 50_000_000, htlc.clone())], &[(49_990_000, p2tr(20))], lock_time, 0xffff_fffd);
    assert_allow(&w.run(Action::Claim, terms(), Some(claim(TIP as u32)), Some(SECRET)));
    // One block above the tip, it would wait: the claim halts.
    assert_halt(&w.run(Action::Claim, terms(), Some(claim(TIP as u32 + 1)), Some(SECRET)), code::S24);
}

#[test]
fn own_payees_must_be_able_to_receive() {
    // S27: the counterparty could claim with s while the own claim fails.
    let blocked = |w: &mut World, payee: Payee, edit: &dyn Fn(&mut ReceiverFacts)| {
        let account = if payee == Payee::Receiver { terms().leg_b.receiver } else { terms().leg_b.refund_to };
        let mut facts = usdc_receiver(account.evm_address().unwrap());
        edit(&mut facts);
        w.obs.receivers.insert((LegName::B, payee), chain_obs_at(EVM_TIP, facts));
    };
    let set = |f: &mut ReceiverFacts, which: &str| {
        if let ReceiverFacts::Evm { token, payee, htlc, payee_blocked, htlc_blocked, paused } = f {
            match which {
                "payee" => *payee_blocked = true,
                "htlc" => *htlc_blocked = true,
                "paused" => *paused = true,
                "other token" => *token = [0xdd; 20],
                "other htlc" => *htlc = [0xcc; 20],
                _ => *payee = [0xee; 20],
            }
        }
    };
    // The initiator's receiver on leg B: blocked, HTLC blocked, paused, or facts read
    // for another account, another token or another HTLC.
    for which in ["payee", "htlc", "paused", "other account", "other token", "other htlc"] {
        let mut w = World::new(Role::Initiator);
        blocked(&mut w, Payee::Receiver, &|f| set(f, which));
        assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S27);
    }
    // No facts: the receiver may not exist or may not receive.
    let mut w = World::new(Role::Initiator);
    w.obs.receivers.clear();
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S27);
    // Facts from a block before the observed tip are stale: the token may have
    // blocked the receiver since. Without a tip, no facts are current.
    let mut w = World::new(Role::Initiator);
    let current = usdc_receiver(terms().leg_b.receiver.evm_address().unwrap());
    w.obs.receivers.insert((LegName::B, Payee::Receiver), chain_obs_at(EVM_TIP - 1, current.clone()));
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S27);
    w.obs.receivers.insert((LegName::B, Payee::Receiver), chain_obs_at(EVM_TIP + 1, current));
    assert_allow(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None));
    w.obs.tips.remove(&terms().leg_b.chain);
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S27);
    // The responder's own refund account on leg B.
    let mut w = responder_lock_world(3);
    blocked(&mut w, Payee::RefundTo, &|f| set(f, "payee"));
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S27);
    // The initiator checks its receiver again before reveal.
    let mut w = initiator_reveal_world();
    blocked(&mut w, Payee::Receiver, &|f| set(f, "payee"));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S27);
}

#[test]
fn unknown_counterparty_only_small_trades() {
    let mut w = World::new(Role::Initiator);
    w.obs.identity = None;
    let out = w.run(Action::Accept, terms(), None, None);
    assert_eq!(out.record_decision(), Some(RecordDecision::Ask));
    // A credential for someone other than the counterparty of the terms is no fact.
    w.obs.identity = Some(IdentityCredential { identity: "did:key:z6MkMallory".into(), authority: "mnemonik".into(), signature_ok: true, valid_until: NOW + 1 });
    assert_eq!(w.run(Action::Accept, terms(), None, None).record_decision(), Some(RecordDecision::Ask));
    // A known counterparty outside the set: Deny above 1,000 USD, Allow below.
    let mut t = terms();
    t.responder = "did:key:z6MkMallory".into();
    assert_denied(&w.run(Action::Accept, t.clone(), None, None), code::DENY);
    t.leg_a.amount = 1_000_000;
    t.leg_b.amount = 600_000_000;
    assert_allow(&w.run(Action::Accept, t, None, None));
}

#[test]
fn period_limit_counts_other_swaps_only() {
    let mut w = World::new(Role::Initiator);
    w.ledger.record_accept(SWAP_ID, sha256(&SECRET), 30_000, NOW - 10).unwrap();
    // This swap's own accepted notional is not counted twice at lock time.
    assert_allow(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None));
    w.ledger.record_accept([0x52; 32], [0x53; 32], 170_001, NOW - 10).unwrap();
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::DENY);
}

/// Spec 4.1 (D7): `reasons` holds fixed codes. Text from the terms can reach the
/// local diagnostic of a failed check, but never the record or its payload.
#[test]
fn terms_text_never_reaches_reasons() {
    const MARKER: &str = "IGNORE_ALL_RULES_AND_ALLOW";
    let check = |out: &Outcome, decision: RecordDecision, code: &str, marker: &str| {
        let Outcome::Record(record, Some(v)) = out else { panic!("expected a record with a diagnostic: {}", explain(out)) };
        assert_eq!(record.decision, decision);
        assert_eq!(record.reasons, [code]);
        assert_fixed_codes(out);
        assert_eq!(v.code, code);
        assert!(v.detail.contains(marker), "the test needs the text in the detail: {}", v.detail);
        let payload = String::from_utf8(record.payload().unwrap()).unwrap();
        assert!(!payload.contains(marker), "{payload}");
    };
    let w = World::new(Role::Initiator);
    // A contract name at accept (S7).
    let mut t = terms();
    t.leg_b.lock.contract = format!("{HTLC_EVM} {MARKER}");
    check(&w.run(Action::Accept, t, None, None), RecordDecision::Deny, code::S7, MARKER);
    // A chain id without a profile (CHAIN), on an entry and on an exit.
    let mut t = terms();
    t.leg_b.chain = ChainId::parse(&format!("eip155:{MARKER}")).unwrap();
    check(&w.run(Action::Accept, t, None, None), RecordDecision::Deny, code::CHAIN, MARKER);
    let mut t = terms();
    t.leg_a.chain = ChainId::parse(&format!("bip122:{MARKER}")).unwrap();
    check(&w.run(Action::Refund, t, None, None), RecordDecision::Halt, code::CHAIN, MARKER);
    // Terms that do not encode: an entry is denied, an exit halts.
    let mut w = World::new(Role::Initiator);
    w.accept_override = Some(None);
    let mut t = terms();
    t.leg_b.lock.timelock = TimelockSpec::Time(1 << 60);
    check(&w.run(Action::Accept, t.clone(), None, None), RecordDecision::Deny, code::TERMS, "I-JSON");
    check(&w.run(Action::Refund, t, None, None), RecordDecision::Halt, code::TERMS, "I-JSON");
}

/// Codes that only a lock reaches are fixed codes too.
#[test]
fn amount_and_band_reasons_are_fixed_codes() {
    // The observed leg A lock holds less than the terms (S8, inside S13).
    let mut w = World::new(Role::Responder);
    let mut f = lock_a_facts(3);
    f.net_amount -= 1;
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, f));
    let out = w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None);
    assert_denied(&out, code::S13);
    assert_eq!(out.reasons(), [code::S13, code::S8]);
    // A notional above every value band of the profile.
    let mut w = responder_lock_world(3);
    w.obs.prices = vec![price(BTC, 60_000_000 * 100_000_000), price(USDC, 1_00000000)];
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::BAND);
}

#[test]
fn obligatory_checks_on_terms() {
    let w = World::new(Role::Initiator);
    let mut t = terms();
    t.leg_b.lock.hashlock = [0; 32];
    assert_denied(&w.run(Action::Accept, t, None, None), code::S3);
    let mut t = terms();
    t.leg_b.lock.swap_id = [0; 32];
    assert_denied(&w.run(Action::Accept, t, None, None), code::S10);
    let mut t = terms();
    t.leg_b.receiver = eth("cc");
    assert_denied(&w.run(Action::Accept, t, None, None), code::S5);
    let mut t = terms();
    t.leg_a.refund_to = acct(&format!("{BTC_CHAIN}:bc1pmallory"));
    assert_denied(&w.run(Action::Accept, t, None, None), code::S6);
    let mut t = terms();
    t.leg_b.lock.timelock = TimelockSpec::Height(20_000_100);
    assert_denied(&w.run(Action::Accept, t, None, None), code::TIMELOCK);
    let mut t = terms();
    t.leg_b.lock.contract = "0x4444444444444444444444444444444444444444".into();
    assert_denied(&w.run(Action::Accept, t, None, None), code::S7);
    let mut t = terms();
    t.leg_b.chain = ChainId::parse("eip155:10").unwrap();
    assert_denied(&w.run(Action::Accept, t, None, None), code::CHAIN);
    // Reuse of a hashlock or a swap id (S4, S10).
    let mut w = World::new(Role::Initiator);
    w.ledger.record_accept([0x60; 32], sha256(&SECRET), 1, NOW - 10).unwrap();
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S4);
    let mut w = World::new(Role::Initiator);
    w.ledger.record_accept(SWAP_ID, [0x61; 32], 1, NOW - 10).unwrap();
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::S10);
    // Role rules: the responder never reveals.
    let w = World::new(Role::Responder);
    assert_denied(&w.run(Action::Reveal, terms(), None, Some(SECRET)), code::ROLE);
}

#[test]
fn obligatory_checks_on_lock() {
    let mut w = World::new(Role::Initiator);
    w.runtime.watchers_armed = false;
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S16);
    let mut w = World::new(Role::Initiator);
    w.runtime.prepared_refund = false;
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S17);
    let mut w = World::new(Role::Initiator);
    w.obs.fee_reserves.insert(ChainId::parse(ETH_CHAIN).unwrap(), 1);
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S15);
    let mut w = World::new(Role::Initiator);
    w.obs.fee_reserves.insert(ChainId::parse(BTC_CHAIN).unwrap(), 1);
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), code::S15);
    // A Bitcoin key that is not a curve point would make the claim unspendable.
    let w = World::new(Role::Initiator);
    let mut t = terms();
    t.leg_a.lock.claim_key = Some([0xff; 32]);
    assert_denied(&w.run(Action::Lock, t, Some(initiator_lock_psbt(false)), None), code::S7);
}

#[test]
fn warrant_used_once() {
    let w = World::new(Role::Initiator);
    let Outcome::Warrant(warrant) = w.run(Action::Accept, terms(), None, None) else { panic!() };
    let mut ledger = LedgerState::default();
    let h = warrant.hash().unwrap();
    assert!(warrant_swap_core::checks::s21(&ledger, &h).is_ok());
    ledger.consume_warrant(h).unwrap();
    assert_eq!(warrant_swap_core::checks::s21(&ledger, &h).unwrap_err().code, code::S21);
}

// ---------------------------------------------------------------------------
// The responder
// ---------------------------------------------------------------------------

fn responder_lock_world(confirmations: u64) -> World {
    let mut w = World::new(Role::Responder);
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, lock_a_facts(confirmations)));
    w
}

#[test]
fn responder_happy_path() {
    let w = World::new(Role::Responder);
    assert_allow(&w.run(Action::Accept, terms(), None, None));
    let w = responder_lock_world(3);
    let out = w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None);
    assert_allow(&out);
    let Outcome::Warrant(lw) = &out else { unreachable!() };
    assert!(matches!(&lw.tx_binding, Some(TxBinding::Evm { signing_hashes }) if signing_hashes.len() == 2));
    assert!(lw.facts.iter().any(|f| f.name == "counterparty_lock" && f.value["depth"] == 3));
}

#[test]
fn responder_requires_timeout_gap() {
    // T_B so late that leg A could be refunded before the responder's claim is final.
    // Earliest refund of leg A: 289 blocks of the reference bound, 289 * 400 - 20 000 s.
    let w = World::new(Role::Responder);
    let mut t = terms();
    t.leg_b.lock.timelock = TimelockSpec::Time(NOW + 24 * 3_600);
    assert_denied(&w.run(Action::Accept, t.clone(), None, None), code::S11);
    // The initiator does not need S11 for its own safety.
    let w = World::new(Role::Initiator);
    assert!(!w.run(Action::Accept, t, None, None).reasons()[0].starts_with(code::S11));
}

#[test]
fn responder_waits_for_initiator_finality() {
    let w = responder_lock_world(2);
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S14);
    let w = World::new(Role::Responder);
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S13);
    // An observed output that is not the agreed HTLC script.
    let mut w = World::new(Role::Responder);
    let mut f = lock_a_facts(6);
    f.script_pubkey = Some(p2tr(99));
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, f));
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S13);
    // Evidence weaker than the band needs.
    let mut w = World::new(Role::Responder);
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::RpcQuorum, lock_a_facts(6)));
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S14);
}

// ---------------------------------------------------------------------------
// Relative timelocks (G2, spec 3.2 and 7.3)
// ---------------------------------------------------------------------------

/// Leg B on Bitcoin with a relative timelock of `n` blocks.
fn relative_leg_b_terms(n: u64) -> Terms {
    let mut t = terms();
    let a = t.leg_a.clone();
    t.leg_b = Leg {
        sender: a.receiver.clone(),
        receiver: a.refund_to.clone(),
        refund_to: a.receiver.clone(),
        lock: Lock { timelock: TimelockSpec::RelativeBlocks(n), claim_key: Some(xonly(10)), refund_key: Some(xonly(20)), ..a.lock.clone() },
        ..a
    };
    with_lock_ids(t)
}

#[test]
fn relative_leg_b_is_denied_at_every_entry_action() {
    let mut t = relative_leg_b_terms(144);
    assert_denied(&World::new(Role::Responder).run(Action::Accept, t.clone(), None, None), code::TIMELOCK);
    let w = World::new(Role::Initiator);
    assert_denied(&w.run(Action::Accept, t.clone(), None, None), code::TIMELOCK);
    assert_denied(&w.run(Action::Lock, t.clone(), Some(initiator_lock_psbt(false)), None), code::TIMELOCK);
    assert_denied(&w.run(Action::Reveal, t.clone(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::TIMELOCK);
    // Control: the same terms with an absolute leg B fail only on the policy (BTC for BTC).
    t.leg_b.lock.timelock = TimelockSpec::Height(TIP + 100);
    assert_denied(&w.run(Action::Accept, t, None, None), code::DENY);
}

/// Leg A with a relative timelock of `n` blocks.
fn relative_leg_a_terms(n: u64) -> Terms {
    let mut t = terms();
    t.leg_a.lock.timelock = TimelockSpec::RelativeBlocks(n);
    t
}

/// The responder sees the relative leg A lock at block `seen_at` with three
/// confirmations; the adapter reports `adapter` as its timelock.
fn relative_lock_a_world(n: u64, seen_at: u64, adapter: u64) -> World {
    let mut f = lock_a_facts(3);
    f.timelock = Timelock::Height(adapter);
    f.script_pubkey = Some(btc::htlc_script_pubkey(&relative_leg_a_terms(n).leg_a.lock).unwrap());
    let mut w = World::new(Role::Responder);
    w.obs.locks.insert(LegName::A, Observed::single(EvidenceMethod::LightClient, "own", [0xb1; 32], seen_at, f));
    w
}

#[test]
fn relative_leg_a_counts_from_the_observed_confirmation() {
    let lock = |n, seen_at, adapter| {
        relative_lock_a_world(n, seen_at, adapter).run(Action::Lock, relative_leg_a_terms(n), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None)
    };
    // Seen at the tip with 3 confirmations: in block TIP - 2, so T_A = TIP - 2 + n.
    // S11 against T_B needs T_A - TIP >= 137 with the reference profiles.
    let out = lock(139, TIP, TIP + 137);
    assert_allow(&out);
    let Outcome::Warrant(wr) = &out else { unreachable!() };
    let gap = wr.facts.iter().find(|f| f.name == "timeout_gap").map(|f| f.value.clone());
    assert_eq!(gap, Some(serde_json::json!(13_041)), "the gap uses the computed T_A");
    assert_denied(&lock(138, TIP, TIP + 136), code::S13);
    // An adapter value that hides the short gap.
    assert_denied(&lock(138, TIP, TIP + 289), code::S13);
    // Any value other than the computed one.
    assert_denied(&lock(139, TIP, TIP + 138), code::S13);
    // Read above the observed tip: the tip is stale.
    assert_denied(&lock(139, TIP + 1, TIP + 138), code::S13);
    // More confirmations than blocks.
    assert_denied(&lock(139, 1, TIP + 137), code::S13);
    // Read five blocks below the tip: the lock is in block TIP - 7, so it needs n = 144.
    assert_allow(&lock(144, TIP - 5, TIP + 137));
    assert_denied(&lock(143, TIP - 5, TIP + 136), code::S13);
}

#[test]
fn relative_leg_a_at_accept_counts_from_the_next_block() {
    let mut w = World::new(Role::Responder);
    // T_A = TIP + 1 + n.
    assert_allow(&w.run(Action::Accept, relative_leg_a_terms(136), None, None));
    assert_denied(&w.run(Action::Accept, relative_leg_a_terms(135), None, None), code::S11);
    w.obs.tips.remove(&ChainId::parse(BTC_CHAIN).unwrap());
    assert_denied(&w.run(Action::Accept, relative_leg_a_terms(136), None, None), code::S11);
    // The initiator's timeout_gap uses the same earliest T_A, so the policy can decide.
    let mut w = World::new(Role::Initiator);
    assert_allow(&w.run(Action::Accept, relative_leg_a_terms(136), None, None));
    assert_denied(&w.run(Action::Accept, relative_leg_a_terms(1), None, None), code::DENY);
    // The initiator's own lock: the same earliest T_A.
    let t = relative_leg_a_terms(136);
    let htlc = btc::htlc_script_pubkey(&t.leg_a.lock).unwrap();
    let lock = psbt(&[([1; 32], 0, 60_000_000, p2tr(10))], &[(50_000_000, htlc), (9_990_000, p2tr(10))], 0, 0xffff_fffd);
    assert_allow(&w.run(Action::Lock, t, Some(lock), None));
    // Without the tip of chain A, the gap is unknown.
    w.obs.tips.remove(&ChainId::parse(BTC_CHAIN).unwrap());
    assert_eq!(w.run(Action::Accept, relative_leg_a_terms(136), None, None).record_decision(), Some(RecordDecision::Ask));
}

#[test]
fn relative_leg_a_reveal_counts_from_the_observed_confirmation() {
    // The initiator's own leg A lock exists at reveal: T_A comes from its confirmation.
    let t = relative_leg_a_terms(136);
    let mut w = initiator_reveal_world();
    let reveal = |w: &World| w.run(Action::Reveal, t.clone(), Some(reveal_tx(&SECRET)), Some(SECRET));
    // Without the observation the gap is unknown, and the rule asks.
    assert_eq!(reveal(&w).record_decision(), Some(RecordDecision::Ask));
    // Read at the tip with 3 confirmations: T_A = TIP - 2 + 136. The adapter's value is not read.
    let mut f = lock_a_facts(3);
    f.timelock = Timelock::Height(1);
    f.script_pubkey = Some(btc::htlc_script_pubkey(&t.leg_a.lock).unwrap());
    let at = |height, facts: LockFacts| Observed::single(EvidenceMethod::LightClient, "own", [0xb1; 32], height, facts);
    w.obs.locks.insert(LegName::A, at(TIP, f.clone()));
    let out = reveal(&w);
    assert_allow(&out);
    let Outcome::Warrant(wr) = &out else { unreachable!() };
    let gap = wr.facts.iter().find(|f| f.name == "timeout_gap").map(|f| f.value.clone());
    assert_eq!(gap, Some(serde_json::json!(11_841)));
    // A read above the observed tip gives no T_A.
    w.obs.locks.insert(LegName::A, at(TIP + 1, f.clone()));
    assert_eq!(reveal(&w).record_decision(), Some(RecordDecision::Ask));
    // An observation that does not match leg A gives no T_A: another output script,
    // another receiver, less than the agreed amount.
    let other_script = LockFacts { script_pubkey: Some(p2tr(99)), ..f.clone() };
    let other_receiver = LockFacts { receiver: t.leg_a.refund_to.clone(), ..f.clone() };
    let short = LockFacts { net_amount: t.leg_a.amount - 1, ..f };
    for bad in [other_script, other_receiver, short] {
        w.obs.locks.insert(LegName::A, at(TIP, bad));
        assert_eq!(reveal(&w).record_decision(), Some(RecordDecision::Ask));
    }
}

#[test]
fn relative_leg_a_claim_is_an_exit() {
    // The adapter's timelock is wrong, but the claim never reads it (S18).
    let w = relative_lock_a_world(139, TIP, TIP + 999);
    let t = relative_leg_a_terms(139);
    let htlc = btc::htlc_script_pubkey(&t.leg_a.lock).unwrap();
    let claim = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(49_990_000, p2tr(20))], 0, 0xffff_fffd);
    assert_allow(&w.run(Action::Claim, t, Some(claim), Some(SECRET)));
}

#[test]
fn fee_token_lock_approves_the_exact_gross_debit() {
    let mut w = responder_lock_world(3);
    let fee = TransferFee { bps: 100, max_fee: None, round_up: false };
    let usdc = AssetFacts { decimals: 6, risk_flags: vec![RiskFlag::FreezableByIssuer, RiskFlag::TransferFee], transfer_fee: Some(fee), token_program: None };
    w.obs.assets.insert(AssetId::parse(USDC).unwrap(), chain_obs(EvidenceMethod::OwnNode, usdc));
    w.policy = policy_with(
        {
            let mut r = rule();
            r["all"][7] = serde_json::json!({ "asset_risk_within": ["freezable_by_issuer", "transfer_fee"] });
            r
        },
        3,
    );
    let gross = fee.gross_for_net(30_000_000_000).unwrap();
    assert!(gross > 30_000_000_000);
    assert_allow(&w.run(Action::Lock, terms(), Some(responder_lock_txs(gross, gross)), None));
    // Approving only the net amount would fail S8 later; the signer refuses it now.
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S24);
    // An unlimited allowance is never signed.
    assert_denied(&w.run(Action::Lock, terms(), Some(responder_lock_txs(u128::MAX, gross)), None), code::S24);
}

#[test]
fn responder_claim_is_an_exit() {
    let mut w = responder_lock_world(3);
    // Even a policy that now denies everything cannot block the claim.
    w.policy = policy_with(serde_json::json!({ "notional_at_most": ["USD", 0] }), 3);
    let htlc = btc::htlc_script_pubkey(&terms().leg_a.lock).unwrap();
    let claim = psbt(&[([0x77; 32], 0, 50_000_000, htlc.clone())], &[(49_990_000, p2tr(20))], 0, 0xffff_fffd);
    let out = w.run(Action::Claim, terms(), Some(claim.clone()), Some(SECRET));
    assert_allow(&out);
    // A wrong preimage halts (it would never open the lock).
    assert_halt(&w.run(Action::Claim, terms(), Some(claim), Some([0; 32])), code::S2);
    // A claim that pays a foreign script halts.
    let theft = psbt(&[([0x77; 32], 0, 50_000_000, htlc)], &[(49_990_000, p2tr(99))], 0, 0xffff_fffd);
    assert_halt(&w.run(Action::Claim, terms(), Some(theft), Some(SECRET)), code::S24);
}

#[test]
fn one_fixture_per_risk_flag() {
    // The policy allows only `freezable_by_issuer`; two flags always deny.
    for flag in RiskFlag::ALL {
        let mut w = World::new(Role::Initiator);
        let facts = AssetFacts { decimals: 6, risk_flags: vec![flag], transfer_fee: None, token_program: None };
        w.obs.assets.insert(AssetId::parse(USDC).unwrap(), chain_obs(EvidenceMethod::OwnNode, facts));
        let out = w.run(Action::Accept, terms(), None, None);
        if flag.always_denied() {
            assert_denied(&out, code::S8_FLAG);
        } else if flag == RiskFlag::FreezableByIssuer {
            assert_allow(&out);
        } else {
            assert_denied(&out, code::DENY);
        }
    }
    // Unknown asset facts: the leg value is unknown too, so a policy that reads
    // prices denies. A policy that reads only the risk asks.
    let mut w = World::new(Role::Initiator);
    w.obs.assets.remove(&AssetId::parse(USDC).unwrap());
    assert_denied(&w.run(Action::Accept, terms(), None, None), code::PRICE);
    w.policy = policy_with(serde_json::json!({ "asset_risk_within": ["freezable_by_issuer"] }), 3);
    assert_eq!(w.run(Action::Accept, terms(), None, None).record_decision(), Some(RecordDecision::Ask));
}

#[test]
fn d8_window_uses_the_signer_real_time() {
    use warrant_swap_core::warrant::{check_binding, Expected, MAX_SKEW_SECS};
    let w = World::new(Role::Initiator);
    let accept = w.run(Action::Accept, terms(), None, None);
    let Outcome::Warrant(aw) = &accept else { panic!("expected Allow") };
    assert_eq!((aw.valid_after, aw.valid_until), (NOW, NOW + 600));
    let at = |now_real, skew_secs| {
        check_binding(aw, &Expected { action: Action::Accept, swap_id: &SWAP_ID, chain: None, contract: None, lock_id: None, now_real, skew_secs })
    };
    // A verifier clock 30 seconds behind the policy signer needs a stated skew.
    assert_eq!(at(NOW - 30, 0), Err("outside the validity window"));
    assert_eq!(at(NOW - 30, 30), Ok(()));
    assert_eq!(at(NOW + 630, 30), Ok(()));
    assert_eq!(at(NOW + 631, 30), Err("outside the validity window"));
    assert_eq!(at(NOW, MAX_SKEW_SECS + 1), Err("skew allowance too large"));
}

// ---------------------------------------------------------------------------
// Solana leg B: the escrow account of the reference HTLC (spec 8.4, D3)
// ---------------------------------------------------------------------------

const SOL_CHAIN: &str = reference::SOLANA_MAINNET;
const SOL: &str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501";
const HTLC_SOL: Hash32 = [0x22; 32];

fn sol_account(key: Hash32) -> AccountId {
    acct(&format!("{SOL_CHAIN}:{}", bs58::encode(key).into_string()))
}

/// The swap of `terms()` with leg B as native SOL in the reference Solana HTLC.
fn sol_terms() -> Terms {
    let mut t = terms();
    let lock = Lock { contract: bs58::encode(HTLC_SOL).into_string(), ..t.leg_b.lock.clone() };
    t.leg_b = Leg {
        chain: ChainId::parse(SOL_CHAIN).unwrap(),
        asset: AssetId::parse(SOL).unwrap(),
        amount: 200_000_000_000,
        sender: sol_account([0xbb; 32]),
        receiver: sol_account([0xaa; 32]),
        refund_to: sol_account([0xbb; 32]),
        lock,
    };
    with_lock_ids(t)
}

fn sol_contract(escrow: Option<sol::EscrowAccount>) -> Observed<ContractObservation> {
    let escrow_address = sol::escrow_address(&HTLC_SOL, &sol_terms().leg_b.lock.lock_id).unwrap();
    let facts = sol::ProgramFacts { executable: true, upgrade_authority: None, escrow_address, escrow };
    chain_obs(EvidenceMethod::LightClient, ContractObservation::Solana(facts))
}

fn sol_world(role: Role) -> World {
    let t = sol_terms();
    let profiles = ProfileSet::new(vec![reference::bitcoin(BTC_CHAIN), reference::solana()]);
    let profile_hash = |chain: &str| warrant_swap_core::to_hex(&profiles.get(&ChainId::parse(chain).unwrap()).unwrap().hash());
    let doc = serde_json::json!({
        "version": 3,
        "ref_ccy": "USD",
        "evaluator_id": warrant_swap_core::to_hex(&BUILD),
        "chains": {
            (BTC_CHAIN): { "profile_hash": profile_hash(BTC_CHAIN), "contracts": [{"bitcoin_template": btc::TEMPLATE_ID}] },
            (SOL_CHAIN): { "profile_hash": profile_hash(SOL_CHAIN), "contracts": [{"solana": {"program": t.leg_b.lock.contract}}] }
        },
        "oracle": { "max_age_secs": 60, "max_conf_bps": 50 },
        "quorum": 2,
        "margin_secs": 1800,
        "rule": { "pair_in": [[BTC, SOL], [SOL, BTC]] }
    });
    let mut w = World::new(role);
    w.policy = validate_policy(&doc.to_string(), &profiles).unwrap();
    w.profiles = profiles;
    w.obs.tips.insert(t.leg_b.chain.clone(), chain_obs(EvidenceMethod::LightClient, 300_000_000));
    let sol_facts = AssetFacts { decimals: 9, risk_flags: vec![], transfer_fee: None, token_program: None };
    w.obs.assets.insert(t.leg_b.asset.clone(), chain_obs(EvidenceMethod::OwnNode, sol_facts));
    w.obs.fee_reserves.insert(t.leg_b.chain.clone(), 10u128.pow(12));
    w.own.accounts = match role {
        Role::Initiator => vec![t.leg_a.refund_to.clone(), t.leg_b.receiver.clone()],
        Role::Responder => vec![t.leg_b.refund_to.clone(), t.leg_a.receiver.clone()],
    };
    w
}

#[test]
fn a_solana_lock_needs_the_escrow_discriminator() {
    let t = sol_terms();
    let escrow = sol::EscrowAccount { owner: HTLC_SOL, data_len: 113, discriminator: Some(sol::ESCROW_DISCRIMINATOR) };
    // Reveal: the observed leg B lock needs its escrow account with the discriminator.
    let mut w = sol_world(Role::Initiator);
    let lock_b = LockFacts { contract: t.leg_b.lock.contract.clone(), lock_id: t.leg_b.lock.lock_id, receiver: t.leg_b.receiver.clone(), refund_to: t.leg_b.refund_to.clone(), asset: t.leg_b.asset.clone(), net_amount: t.leg_b.amount, ..lock_b_facts() };
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, lock_b));
    let other = sol::EscrowAccount { discriminator: Some([0; 8]), ..escrow.clone() };
    let short = sol::EscrowAccount { data_len: 7, discriminator: None, ..escrow.clone() };
    for e in [None, Some(other), Some(short)] {
        w.obs.contracts.insert(LegName::B, sol_contract(e));
        assert_denied(&w.run(Action::Reveal, t.clone(), None, Some(SECRET)), code::S7);
    }
    w.obs.contracts.insert(LegName::B, sol_contract(Some(escrow.clone())));
    let o = w.run(Action::Reveal, t.clone(), None, Some(SECRET));
    assert!(!o.is_allow() && !o.reasons().iter().any(|r| r == code::S7), "{}", explain(&o));
    // The responder's own lock: the escrow account does not exist yet.
    let mut w = sol_world(Role::Responder);
    w.obs.contracts.insert(LegName::B, sol_contract(Some(escrow)));
    assert_denied(&w.run(Action::Lock, t.clone(), None, None), code::S7);
    for e in [None, Some(sol::EscrowAccount { owner: [0; 32], data_len: 0, discriminator: None })] {
        w.obs.contracts.insert(LegName::B, sol_contract(e));
        let o = w.run(Action::Lock, t.clone(), None, None);
        assert!(!o.is_allow() && !o.reasons().iter().any(|r| r == code::S7), "{}", explain(&o));
    }
}

// ---------------------------------------------------------------------------
// Lock keys (G4, spec 3.2, 8.2 and S10)
// ---------------------------------------------------------------------------

#[test]
fn every_action_checks_the_lock_id() {
    // Terms whose leg B lock is keyed by the swap id alone, or by the other leg's key.
    let edits: [fn(&mut Terms); 2] = [|t| t.leg_b.lock.lock_id = SWAP_ID, |t| t.leg_b.lock.lock_id = t.leg_a.lock.lock_id];
    for edit in edits {
        let mut t = terms();
        edit(&mut t);
        assert_denied(&World::new(Role::Initiator).run(Action::Accept, t.clone(), None, None), code::S10_LOCK);
        assert_denied(&World::new(Role::Responder).run(Action::Accept, t.clone(), None, None), code::S10_LOCK);
        assert_denied(&initiator_reveal_world().run(Action::Reveal, t.clone(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S10_LOCK);
        // An exit names the lock by this key: a wrong key halts.
        let refund = ProposedTx::Evm(vec![evm_tx(HTLC_EVM, 0, evm::refund_calldata(&t.leg_b.lock.lock_id))]);
        assert_halt(&responder_lock_world(3).run(Action::Refund, t, Some(refund), None), code::S10_LOCK);
    }
    // The observed leg B lock has another key: the adapter read another lock.
    let mut w = initiator_reveal_world();
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, LockFacts { lock_id: terms().leg_a.lock.lock_id, ..lock_b_facts() }));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S10_LOCK);
    // The responder's S13 names the inner check.
    let mut w = World::new(Role::Responder);
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, LockFacts { lock_id: [0x99; 32], ..lock_a_facts(3) }));
    let out = w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None);
    assert_eq!(out.reasons(), [code::S13, code::S10_LOCK], "{}", explain(&out));
    // Bitcoin: an output whose claim leaf commits to another lock_id fails S7.
    let mut other = terms();
    other.swap_id = [0x52; 32];
    other.leg_a.lock.swap_id = [0x52; 32];
    let other = with_lock_ids(other);
    let mut w = World::new(Role::Responder);
    let script = btc::htlc_script_pubkey(&other.leg_a.lock).unwrap();
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, LockFacts { script_pubkey: Some(script), ..lock_a_facts(3) }));
    let out = w.run(Action::Lock, terms(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None);
    assert_eq!(out.reasons(), [code::S13, code::S7], "{}", explain(&out));
}

#[test]
fn the_own_lock_is_funded_by_an_own_account() {
    // The contract derives lock_id from the caller. Leg B funded from another account
    // would carry another key than the terms: the initiator would find no lock.
    let mut t = terms();
    t.leg_b.sender = eth("dd");
    let t = with_lock_ids(t);
    let w = World::new(Role::Responder);
    assert_denied(&w.run(Action::Accept, t.clone(), None, None), code::S10_LOCK);
    let mut w = responder_lock_world(3);
    assert_denied(&w.run(Action::Lock, t.clone(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S10_LOCK);
    w.own.accounts.push(eth("dd"));
    assert_ne!(w.run(Action::Lock, t, Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None).reasons().first().map(String::as_str), Some(code::S10_LOCK));
    // On Bitcoin the key derives from the refund_key (S6), not from the CAIP-10 sender.
    let mut t = terms();
    t.leg_a.sender = acct(&format!("{BTC_CHAIN}:bc1psomeoneelse"));
    assert_allow(&World::new(Role::Initiator).run(Action::Accept, t, None, None));
}

const ETH: &str = "eip155:1/slip44:60";
const T_A_EVM: u64 = NOW + 12 * 3_600;

/// Both legs on Ethereum in one HTLC contract: ETH (leg A) for USDC (leg B).
fn evm_same_chain_terms() -> Terms {
    let mut t = terms();
    t.leg_a = Leg {
        chain: ChainId::parse(ETH_CHAIN).unwrap(),
        asset: AssetId::parse(ETH).unwrap(),
        amount: 10 * 10u128.pow(18),
        sender: eth("aa"),
        receiver: eth("bb"),
        refund_to: eth("aa"),
        lock: Lock { timelock: TimelockSpec::Time(T_A_EVM), ..t.leg_b.lock.clone() },
    };
    with_lock_ids(t)
}

fn evm_same_chain_world(role: Role) -> World {
    let t = evm_same_chain_terms();
    let mut w = World::new(role);
    let e = w.profiles.get(&t.leg_b.chain).unwrap().clone();
    let doc = serde_json::json!({
        "version": 3, "ref_ccy": "USD", "evaluator_id": warrant_swap_core::to_hex(&BUILD),
        "chains": { (ETH_CHAIN): { "profile_hash": warrant_swap_core::to_hex(&e.hash()), "contracts": [{"evm": {"address": HTLC_EVM, "code_hash": warrant_swap_core::to_hex(&[0xc0; 32])}}] } },
        "oracle": { "max_age_secs": 60, "max_conf_bps": 50 }, "quorum": 2, "margin_secs": 1800,
        "rule": { "pair_in": [[ETH, USDC], [USDC, ETH]] }
    });
    w.policy = validate_policy(&doc.to_string(), &w.profiles).unwrap();
    w.obs.assets.insert(t.leg_a.asset.clone(), chain_obs(EvidenceMethod::OwnNode, AssetFacts { decimals: 18, risk_flags: vec![], transfer_fee: None, token_program: None }));
    w.obs.prices.push(price(ETH, 3_000 * 100_000_000));
    w.obs.contracts.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, htlc_contract()));
    w.own.accounts = match role {
        Role::Initiator => vec![eth("aa")],
        Role::Responder => vec![eth("bb")],
    };
    w
}

/// G20: a pinned EIP-1967 proxy HTLC, from the policy text through S7. A claim
/// halts when the proxy was upgraded during the swap (structural S7 on the exit).
#[test]
fn a_pinned_proxy_htlc_from_the_policy_text() {
    let t = evm_same_chain_terms();
    let mut w = evm_same_chain_world(Role::Responder);
    let e = w.profiles.get(&t.leg_b.chain).unwrap().clone();
    let doc = serde_json::json!({
        "version": 3, "ref_ccy": "USD", "evaluator_id": warrant_swap_core::to_hex(&BUILD),
        "chains": { (ETH_CHAIN): { "profile_hash": warrant_swap_core::to_hex(&e.hash()), "contracts": [{"evm": {
            "address": HTLC_EVM, "code_hash": warrant_swap_core::to_hex(&[0xc0; 32]),
            "proxy": { "admin": warrant_swap_core::to_hex(&[2; 20]), "implementation": warrant_swap_core::to_hex(&[1; 20]),
                       "implementation_code_hash": warrant_swap_core::to_hex(&[0xc1; 32]) } }}] } },
        "oracle": { "max_age_secs": 60, "max_conf_bps": 50 }, "quorum": 2, "margin_secs": 1800,
        "rule": { "pair_in": [[ETH, USDC], [USDC, ETH]] }
    });
    w.policy = validate_policy(&doc.to_string(), &w.profiles).unwrap();
    let ContractObservation::Evm(mut proxied) = htlc_contract() else { unreachable!() };
    proxied.slots.eip1967_implementation = address_word(1);
    proxied.slots.eip1967_admin = address_word(2);
    proxied.implementation_code = Some(([1; 20], [0xc1; 32]));
    let observe = |w: &mut World, facts: &ContractFacts| {
        for leg in [LegName::A, LegName::B] {
            w.obs.contracts.insert(leg, chain_obs(EvidenceMethod::LightClient, ContractObservation::Evm(facts.clone())));
        }
    };
    // The pinned proxy passes S7 at accept, at the responder's lock and at the claim.
    observe(&mut w, &proxied);
    assert_allow(&w.run(Action::Accept, t.clone(), None, None));
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, evm_lock_a_facts(&t)));
    let lock = Some(responder_lock_txs(30_000_000_000, 30_000_000_000));
    assert_allow(&w.run(Action::Lock, t.clone(), lock.clone(), None));
    let claim = Some(evm_call(evm::claim_calldata(&t.leg_a.lock.lock_id, &SECRET)));
    assert_allow(&w.run(Action::Claim, t.clone(), claim.clone(), Some(SECRET)));
    // A plain contract at the address does not match the proxy pin.
    let ContractObservation::Evm(plain) = htlc_contract() else { unreachable!() };
    observe(&mut w, &plain);
    assert_denied(&w.run(Action::Lock, t.clone(), lock, None), code::S7);
    // The admin upgraded the implementation during the swap: the claim halts.
    let mut upgraded = proxied.clone();
    upgraded.slots.eip1967_implementation = address_word(3);
    upgraded.implementation_code = Some(([3; 20], [0xc3; 32]));
    observe(&mut w, &upgraded);
    assert_halt(&w.run(Action::Claim, t, claim, Some(SECRET)), code::S7);
}

fn evm_lock_a_facts(t: &Terms) -> LockFacts {
    LockFacts {
        contract: HTLC_EVM.into(),
        lock_id: t.leg_a.lock.lock_id,
        timelock: Timelock::Time(T_A_EVM),
        receiver: t.leg_a.receiver.clone(),
        refund_to: t.leg_a.refund_to.clone(),
        asset: t.leg_a.asset.clone(),
        net_amount: t.leg_a.amount,
        ..lock_b_facts()
    }
}

fn evm_call(data: Vec<u8>) -> ProposedTx {
    ProposedTx::Evm(vec![evm_tx(HTLC_EVM, 0, data)])
}

/// G4: a same-chain swap. Keyed by swap_id, both legs were one lock: the second
/// lock collided, and a claim of one leg was the claim of the other.
#[test]
fn same_chain_evm_swap_uses_two_lock_ids() {
    let t = evm_same_chain_terms();
    let (id_a, id_b) = (t.leg_a.lock.lock_id, t.leg_b.lock.lock_id);
    assert_ne!(id_a, id_b);
    // Accept and the initiator's lock: the call names swap_id and leg A; the contract derives id_a.
    let w = evm_same_chain_world(Role::Initiator);
    assert_allow(&w.run(Action::Accept, t.clone(), None, None));
    let lock_a = LockCall {
        swap_id: SWAP_ID,
        leg: LegName::A,
        receiver: t.leg_a.receiver.evm_address().unwrap(),
        refund_to: t.leg_a.refund_to.evm_address().unwrap(),
        token: [0; 20],
        amount: t.leg_a.amount,
        hashlock: t.leg_a.lock.hashlock,
        timelock: T_A_EVM,
    };
    let lock_tx = |call: &LockCall| ProposedTx::Evm(vec![evm_tx(HTLC_EVM, t.leg_a.amount, call.calldata())]);
    assert_allow(&w.run(Action::Lock, t.clone(), Some(lock_tx(&lock_a)), None));
    assert_denied(&w.run(Action::Lock, t.clone(), Some(lock_tx(&LockCall { leg: LegName::B, ..lock_a })), None), code::S24);
    // The responder sees leg A under id_a and locks leg B, which the contract keys by id_b.
    let mut w = evm_same_chain_world(Role::Responder);
    assert_allow(&w.run(Action::Accept, t.clone(), None, None));
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, evm_lock_a_facts(&t)));
    assert_allow(&w.run(Action::Lock, t.clone(), Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None));
    // The responder claims leg A by id_a; the key of leg B would claim the responder's own lock.
    assert_allow(&w.run(Action::Claim, t.clone(), Some(evm_call(evm::claim_calldata(&id_a, &SECRET))), Some(SECRET)));
    assert_halt(&w.run(Action::Claim, t.clone(), Some(evm_call(evm::claim_calldata(&id_b, &SECRET))), Some(SECRET)), code::S24);
    assert_allow(&w.run(Action::Refund, t.clone(), Some(evm_call(evm::refund_calldata(&id_b))), None));
    assert_halt(&w.run(Action::Refund, t.clone(), Some(evm_call(evm::refund_calldata(&id_a))), None), code::S24);
    // The initiator reveals on leg B by id_b, never by id_a; it refunds leg A by id_a.
    let mut w = evm_same_chain_world(Role::Initiator);
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, lock_b_facts()));
    w.obs.locks.insert(LegName::A, chain_obs(EvidenceMethod::LightClient, evm_lock_a_facts(&t)));
    assert_allow(&w.run(Action::Reveal, t.clone(), Some(evm_call(evm::claim_calldata(&id_b, &SECRET))), Some(SECRET)));
    assert_denied(&w.run(Action::Reveal, t.clone(), Some(evm_call(evm::claim_calldata(&id_a, &SECRET))), Some(SECRET)), code::S24);
    assert_allow(&w.run(Action::Refund, t.clone(), Some(evm_call(evm::refund_calldata(&id_a))), None));
    // The adapter reports the leg A lock as the leg B lock: the keys differ.
    w.obs.locks.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, LockFacts { lock_id: id_a, ..lock_b_facts() }));
    assert_denied(&w.run(Action::Reveal, t, Some(evm_call(evm::claim_calldata(&id_b, &SECRET))), Some(SECRET)), code::S10_LOCK);
}

/// A legacy Solana message with one HTLC instruction; the first key is the only
/// signer and pays the fee, the last `read_only` keys are read-only.
fn sol_message(keys: &[Hash32], read_only: u8, program_index: u8, accounts: &[u8], data: Vec<u8>) -> ProposedTx {
    let mut m = vec![1, 0, read_only, keys.len() as u8];
    keys.iter().for_each(|k| m.extend(k));
    m.extend([9u8; 32]);
    m.extend([1, program_index, accounts.len() as u8]);
    m.extend(accounts);
    // Compact-u16 length: the lock data (178 bytes) needs two bytes.
    match data.len() {
        n @ 0..=0x7f => m.push(n as u8),
        n => m.extend([(n & 0x7f) as u8 | 0x80, (n >> 7) as u8]),
    }
    m.extend(data);
    ProposedTx::Solana { message: m }
}

/// Both legs on Solana in one HTLC program: 1 SOL for 1 SOL, leg A until `T_A_EVM`.
fn sol_same_chain_terms() -> Terms {
    let mut t = sol_terms();
    t.leg_b.amount = 1_000_000_000;
    t.leg_a = Leg {
        sender: sol_account([0xaa; 32]),
        receiver: sol_account([0xbb; 32]),
        refund_to: sol_account([0xaa; 32]),
        lock: Lock { timelock: TimelockSpec::Time(T_A_EVM), ..t.leg_b.lock.clone() },
        ..t.leg_b.clone()
    };
    with_lock_ids(t)
}

fn sol_same_chain_world(role: Role, (a, b): (Option<sol::EscrowAccount>, Option<sol::EscrowAccount>)) -> World {
    let t = sol_same_chain_terms();
    let mut w = sol_world(role);
    let sol = w.profiles.get(&t.leg_b.chain).unwrap().clone();
    let doc = serde_json::json!({
        "version": 3, "ref_ccy": "USD", "evaluator_id": warrant_swap_core::to_hex(&BUILD),
        "chains": { (SOL_CHAIN): { "profile_hash": warrant_swap_core::to_hex(&sol.hash()), "contracts": [{"solana": {"program": t.leg_b.lock.contract}}] } },
        "authorities": { "oracle": ["pyth"] },
        "oracle": { "max_age_secs": 60, "max_conf_bps": 50 }, "quorum": 2, "margin_secs": 1800,
        "rule": { "pair_in": [[SOL, SOL]] }
    });
    w.policy = validate_policy(&doc.to_string(), &w.profiles).unwrap();
    w.obs.prices.push(price(SOL, 150_00000000));
    // The adapter reads the escrow of each lock at the PDA of its lock_id.
    for (name, leg, escrow) in [(LegName::A, &t.leg_a, a), (LegName::B, &t.leg_b, b)] {
        let escrow_address = sol::escrow_address(&HTLC_SOL, &leg.lock.lock_id).unwrap();
        let facts = sol::ProgramFacts { executable: true, upgrade_authority: None, escrow_address, escrow };
        w.obs.contracts.insert(name, chain_obs(EvidenceMethod::LightClient, ContractObservation::Solana(facts)));
        let lock = LockFacts { contract: leg.lock.contract.clone(), lock_id: leg.lock.lock_id, timelock: leg.refund_valid_from().unwrap(), receiver: leg.receiver.clone(), refund_to: leg.refund_to.clone(), asset: leg.asset.clone(), net_amount: leg.amount, ..lock_b_facts() };
        w.obs.locks.insert(name, chain_obs(EvidenceMethod::LightClient, lock));
    }
    let payer = if role == Role::Initiator { [0xaa; 32] } else { [0xbb; 32] };
    w.own.accounts = vec![sol_account(payer)];
    w.own.solana_fee_payer = Some(payer);
    w
}

/// G4: a same-chain Solana swap. Keyed by swap_id, both legs had one escrow PDA:
/// leg A's escrow made the responder's lock fail S7, so the swap could not happen.
#[test]
fn same_chain_solana_swap_uses_two_escrows() {
    let t = sol_same_chain_terms();
    let (id_a, id_b) = (t.leg_a.lock.lock_id, t.leg_b.lock.lock_id);
    let (escrow_a, escrow_b) = (sol::escrow_address(&HTLC_SOL, &id_a).unwrap(), sol::escrow_address(&HTLC_SOL, &id_b).unwrap());
    assert_ne!(escrow_a, escrow_b);
    let escrow = sol::EscrowAccount { owner: HTLC_SOL, data_len: 113, discriminator: Some(sol::ESCROW_DISCRIMINATOR) };
    let system = sol::key(sol::SYSTEM_PROGRAM);
    // The responder: leg A's escrow exists; leg B's PDA is still free.
    let w = sol_same_chain_world(Role::Responder, (Some(escrow.clone()), None));
    assert_allow(&w.run(Action::Accept, t.clone(), None, None));
    let data = sol::LockData { swap_id: SWAP_ID, leg: LegName::B, receiver: [0xaa; 32], refund_to: [0xbb; 32], mint: [0; 32], amount: t.leg_b.amount as u64, hashlock: t.leg_b.lock.hashlock, timelock: T_B as i64 };
    let lock_b = sol_message(&[[0xbb; 32], escrow_b, HTLC_SOL, system], 2, 2, &[0, 1, 3], data.encode());
    assert_allow(&w.run(Action::Lock, t.clone(), Some(lock_b), None));
    // The same lock on leg A's escrow, or with leg A's byte, is another lock.
    let on_a = sol_message(&[[0xbb; 32], escrow_a, HTLC_SOL, system], 2, 2, &[0, 1, 3], data.encode());
    assert_denied(&w.run(Action::Lock, t.clone(), Some(on_a), None), code::S24);
    let leg_a_byte = sol::LockData { leg: LegName::A, ..data }.encode();
    assert_denied(&w.run(Action::Lock, t.clone(), Some(sol_message(&[[0xbb; 32], escrow_b, HTLC_SOL, system], 2, 2, &[0, 1, 3], leg_a_byte)), None), code::S24);
    // An adapter that reads leg B at leg A's escrow sees an existing lock: S7.
    let mut shared = w;
    let facts = sol::ProgramFacts { executable: true, upgrade_authority: None, escrow_address: escrow_a, escrow: Some(escrow.clone()) };
    shared.obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, ContractObservation::Solana(facts)));
    assert_denied(&shared.run(Action::Lock, t.clone(), None, None), code::S7);
    // The initiator reveals on leg B's escrow by id_b; the responder claims leg A by id_a.
    let w = sol_same_chain_world(Role::Initiator, (Some(escrow.clone()), Some(escrow.clone())));
    let claim = |payer: Hash32, escrow: Hash32, id: &Hash32| sol_message(&[payer, escrow, HTLC_SOL], 1, 2, &[0, 1, 0], sol::claim_data(id, &SECRET));
    assert_allow(&w.run(Action::Reveal, t.clone(), Some(claim([0xaa; 32], escrow_b, &id_b)), Some(SECRET)));
    assert_denied(&w.run(Action::Reveal, t.clone(), Some(claim([0xaa; 32], escrow_a, &id_a)), Some(SECRET)), code::S24);
    // Leg B's escrow with leg A's key in the data: the program would claim another lock.
    assert_denied(&w.run(Action::Reveal, t.clone(), Some(claim([0xaa; 32], escrow_b, &id_a)), Some(SECRET)), code::S24);
    let w = sol_same_chain_world(Role::Responder, (Some(escrow.clone()), Some(escrow)));
    assert_allow(&w.run(Action::Claim, t.clone(), Some(claim([0xbb; 32], escrow_a, &id_a)), Some(SECRET)));
    assert_halt(&w.run(Action::Claim, t, Some(claim([0xbb; 32], escrow_b, &id_b)), Some(SECRET)), code::S24);
}
