//! End-to-end tests of the authorization pipeline: a BTC (Bitcoin, leg A) for USDC
//! (Ethereum, leg B) swap, from both sides. Each obligatory check has a test in
//! which only that check fails; the fault-injection tests of spec 13.3 that do
//! not need a running signer are here too (1, 2, 3, 5, 6, 7, 8, 10).

use bitcoin as rb;
use rb::hashes::Hash as _;
use warrant_swap_core::authorize::{authorize, AcceptEvidence, Env, IdentityCredential, Observations, Outcome, Request};
use warrant_swap_core::caip::{AccountId, AssetId, ChainId};
use warrant_swap_core::checks::{code, AssetFacts, ContractObservation, LockFacts, Payee, ReceiverFacts, Runtime};
use warrant_swap_core::dsl::{validate_policy, CompiledPolicy};
use warrant_swap_core::evm::{self, ContractFacts, Eip1559Tx, LockCall};
use warrant_swap_core::facts::{EvidenceMethod, Observed, PriceReport, Report, TransferFee};
use warrant_swap_core::ledger::LedgerState;
use warrant_swap_core::profile::{reference, ProfileSet};
use warrant_swap_core::tx::{OwnAccounts, ProposedTx};
use warrant_swap_core::types::{Action, HashAlg, HtlcKeys, Leg, LegName, Lock, RiskFlag, Role, Terms, TimelockSpec};
use warrant_swap_core::verified::Timelock;
use warrant_swap_core::warrant::{RecordDecision, TxBinding};
use warrant_swap_core::{bitcoin as btc, Hash32};

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

fn terms() -> Terms {
    let h = sha256(&SECRET);
    Terms {
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
                keys: Some(HtlcKeys { receiver: xonly(20), refund: xonly(10) }),
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
                keys: None,
            },
        },
    }
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
        swap_id: SWAP_ID,
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
        swap_id: SWAP_ID,
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
    ContractObservation::Evm(ContractFacts { code_hash: [0xc0; 32], proxy_implementation: None, proxy_admin: None })
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

fn assert_allow(o: &Outcome) {
    assert!(o.is_allow(), "expected Allow, got {:?}: {}", o.record_decision(), reason(o));
}

fn assert_denied(o: &Outcome, code: &str) {
    assert_eq!(o.record_decision(), Some(RecordDecision::Deny), "{}", reason(o));
    assert!(reason(o).starts_with(code), "expected {code}, got {}", reason(o));
}

fn assert_halt(o: &Outcome, code: &str) {
    assert_eq!(o.record_decision(), Some(RecordDecision::Halt), "{}", reason(o));
    assert!(reason(o).starts_with(code), "expected {code}, got {}", reason(o));
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
    ProposedTx::Evm(vec![evm_tx(HTLC_EVM, 0, evm::claim_calldata(&SWAP_ID, preimage))])
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
    // A proxy HTLC.
    let mut w = initiator_reveal_world();
    let proxy = ContractObservation::Evm(ContractFacts { code_hash: [0xc0; 32], proxy_implementation: Some([1; 20]), proxy_admin: Some([2; 20]) });
    w.obs.contracts.insert(LegName::B, chain_obs(EvidenceMethod::LightClient, proxy));
    assert_denied(&w.run(Action::Reveal, terms(), Some(reveal_tx(&SECRET)), Some(SECRET)), code::S7);
    // An asset whose issuer can seize it is outside the allowed flags: policy Deny.
    let mut w = World::new(Role::Initiator);
    let seizable = AssetFacts { decimals: 6, risk_flags: vec![RiskFlag::SeizableByIssuer], transfer_fee: None, token_program: None };
    w.obs.assets.insert(AssetId::parse(USDC).unwrap(), chain_obs(EvidenceMethod::OwnNode, seizable));
    assert_denied(&w.run(Action::Accept, terms(), None, None), "POLICY_DENY");
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
    assert_eq!(out.reasons(), ["EXIT_ACTION"]);
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
}

#[test]
fn bad_price_denies() {
    let mut w = World::new(Role::Initiator);
    w.obs.prices = vec![price(BTC, 61_000_00000000), price(USDC, 1_00000000)];
    assert_denied(&w.run(Action::Accept, terms(), None, None), "POLICY_DENY");
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
    stolen.leg_a.lock.keys = Some(HtlcKeys { receiver: xonly(10), refund: xonly(10) });
    let w = responder_lock_world(3);
    assert_denied(&w.run(Action::Accept, stolen.clone(), None, None), code::S5);
    assert_denied(&w.run(Action::Lock, stolen, Some(responder_lock_txs(30_000_000_000, 30_000_000_000)), None), code::S5);
    // The initiator's own refund key must be its own, or it cannot refund leg A.
    let mut lost = terms();
    lost.leg_a.lock.keys = Some(HtlcKeys { receiver: xonly(20), refund: xonly(20) });
    let w = World::new(Role::Initiator);
    assert_denied(&w.run(Action::Accept, lost.clone(), None, None), code::S6);
    assert_denied(&w.run(Action::Lock, lost, Some(initiator_lock_psbt(false)), None), code::S6);
    // A Bitcoin leg without leaf keys has no claim key and no refund key.
    let mut keyless = terms();
    keyless.leg_a.lock.keys = None;
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
        assert!(reason(o).contains(code::S7), "{}", reason(o));
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
    assert_denied(&w.run(Action::Accept, t.clone(), None, None), "POLICY_DENY");
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
    assert_denied(&w.run(Action::Lock, terms(), Some(initiator_lock_psbt(false)), None), "POLICY_DENY");
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
    t.leg_a.lock.keys.as_mut().unwrap().receiver = [0xff; 32];
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
            assert_denied(&out, "POLICY_DENY");
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
        check_binding(aw, &Expected { action: Action::Accept, swap_id: &SWAP_ID, chain: None, contract: None, now_real, skew_secs })
    };
    // A verifier clock 30 seconds behind the policy signer needs a stated skew.
    assert_eq!(at(NOW - 30, 0), Err("outside the validity window"));
    assert_eq!(at(NOW - 30, 30), Ok(()));
    assert_eq!(at(NOW + 630, 30), Ok(()));
    assert_eq!(at(NOW + 631, 30), Err("outside the validity window"));
    assert_eq!(at(NOW, MAX_SKEW_SECS + 1), Err("skew allowance too large"));
}
