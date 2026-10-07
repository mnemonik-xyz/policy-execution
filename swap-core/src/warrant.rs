//! The swap warrant and the decision record (spec section 4). Payloads are JCS
//! (RFC 8785) with the encodings of implementation spec section 1. Signing is the
//! job of the signer (W3); this module produces the exact bytes to sign.

use crate::enc;
use crate::facts::FactRecord;
use crate::jcs::{self, JcsError};
use crate::types::{Action, Leg};
use crate::Hash32;
use serde::{Deserialize, Serialize};

pub const PROTOCOL: &str = "warrant.swap.v1";
pub const DECISION_PROTOCOL: &str = "warrant.swap.decision.v1";

/// The evaluator build that decided, or a human owner's review of an `Ask`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvaluatorId {
    Build(Hash32),
    HumanReview,
}

impl Serialize for EvaluatorId {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            EvaluatorId::Build(h) => s.serialize_str(&crate::to_hex(h)),
            EvaluatorId::HumanReview => s.serialize_str("human-review"),
        }
    }
}

impl<'de> Deserialize<'de> for EvaluatorId {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let text = String::deserialize(d)?;
        if text == "human-review" {
            return Ok(EvaluatorId::HumanReview);
        }
        crate::from_hex_array(&text)
            .map(EvaluatorId::Build)
            .ok_or_else(|| serde::de::Error::custom("expected an evaluator hash or \"human-review\""))
    }
}

/// The exact transaction or transactions that the signer signs (spec 4.2).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "family", rename_all = "snake_case", deny_unknown_fields)]
pub enum TxBinding {
    Bitcoin {
        txid: String,
        #[serde(with = "enc::hex32_vec")]
        sighashes: Vec<Hash32>,
        sighash_types: Vec<u8>,
    },
    /// Signing hashes in order; a token lock binds the exact `approve` and the lock.
    Evm {
        #[serde(with = "enc::hex32_vec")]
        signing_hashes: Vec<Hash32>,
    },
    Solana {
        #[serde(with = "enc::hex32")]
        message_hash: Hash32,
    },
}

/// An authorization of exactly one action for one swap. Only `Allow` produces one.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SwapWarrant {
    pub protocol: String,
    pub action: Action,
    #[serde(with = "enc::hex32")]
    pub swap_id: Hash32,
    #[serde(with = "enc::hex32")]
    pub terms_hash: Hash32,
    #[serde(with = "enc::hex32")]
    pub inner_sig_hash: Hash32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub leg: Option<Leg>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tx_binding: Option<TxBinding>,
    pub facts: Vec<FactRecord>,
    #[serde(with = "enc::hex32")]
    pub policy_hash: Hash32,
    pub policy_version: u64,
    pub evaluator_id: EvaluatorId,
    pub decision: String,
    /// Fixed reason codes from `checks::code::ALL` only, never detail text (spec 4.1).
    pub reasons: Vec<String>,
    pub valid_after: u64,
    pub valid_until: u64,
    #[serde(with = "enc::hex16")]
    pub nonce: [u8; 16],
    #[serde(with = "enc::hex32_opt")]
    pub prev_warrant: Option<Hash32>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordDecision {
    Deny,
    Ask,
    /// An exit action failed a structural check: the signer stops and alerts the owner.
    Halt,
}

/// A `Deny`, `Ask` or `Halt` result. Signed under a different protocol value, so a
/// verifier can never mistake it for an authorization.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecisionRecord {
    pub protocol: String,
    pub action: Action,
    #[serde(with = "enc::hex32")]
    pub swap_id: Hash32,
    #[serde(with = "enc::hex32")]
    pub terms_hash: Hash32,
    pub decision: RecordDecision,
    /// Fixed reason codes from `checks::code::ALL` only, never detail text (spec 4.1).
    pub reasons: Vec<String>,
    pub facts: Vec<FactRecord>,
    #[serde(with = "enc::hex32")]
    pub policy_hash: Hash32,
    pub policy_version: u64,
    pub evaluator_id: EvaluatorId,
    pub at: u64,
    #[serde(with = "enc::hex16")]
    pub nonce: [u8; 16],
    #[serde(with = "enc::hex32_opt")]
    pub prev_warrant: Option<Hash32>,
}

impl SwapWarrant {
    /// The bytes that the signer signs and Mnemonik anchors.
    pub fn payload(&self) -> Result<Vec<u8>, JcsError> {
        jcs::to_vec(self)
    }

    /// `blake3(payload)`; the next record of this swap and party links to it.
    pub fn hash(&self) -> Result<Hash32, JcsError> {
        Ok(crate::blake3(&self.payload()?))
    }
}

impl DecisionRecord {
    pub fn payload(&self) -> Result<Vec<u8>, JcsError> {
        jcs::to_vec(self)
    }

    pub fn hash(&self) -> Result<Hash32, JcsError> {
        Ok(crate::blake3(&self.payload()?))
    }
}

/// The largest clock skew allowance, in seconds, that a verifier can state (S20).
/// A larger stated skew is rejected: it would extend the validity window too far.
pub const MAX_SKEW_SECS: u64 = 60;

/// What a verifier expects a warrant to authorize.
#[derive(Clone, Copy)]
pub struct Expected<'a> {
    pub action: Action,
    pub swap_id: &'a Hash32,
    pub chain: Option<&'a crate::caip::ChainId>,
    pub contract: Option<&'a str>,
    /// The key of the lock that the action touches (spec 3.2), with `chain` and
    /// `contract`; `None` for `accept`.
    pub lock_id: Option<&'a Hash32>,
    /// The verifier's real time (Unix seconds), never chain time. One clock for
    /// every action, `accept` included (spec 4.1).
    pub now_real: u64,
    /// The stated clock skew allowance between the verifier and the policy signer,
    /// at most `MAX_SKEW_SECS`.
    pub skew_secs: u64,
}

/// S20: the warrant binds chain id, contract, swap id, lock id, nonce and validity window,
/// and it is an authorization, not a decision record. The window is in the policy
/// signer's real time; the verifier accepts it at its own real time within the
/// stated skew: `valid_after - skew <= now_real <= valid_until + skew`.
pub fn check_binding(w: &SwapWarrant, e: &Expected) -> Result<(), &'static str> {
    if w.protocol != PROTOCOL || w.decision != "allow" {
        return Err("not an authorization");
    }
    if w.action != e.action {
        return Err("other action");
    }
    if &w.swap_id != e.swap_id {
        return Err("other swap");
    }
    match (&w.leg, e.chain, e.contract, e.lock_id) {
        (None, None, None, None) => {}
        (Some(leg), Some(chain), Some(contract), Some(lock_id)) => {
            if &leg.chain != chain {
                return Err("other chain");
            }
            if leg.lock.contract != contract {
                return Err("other contract");
            }
            if &leg.lock.swap_id != e.swap_id {
                return Err("lock of another swap");
            }
            if &leg.lock.lock_id != lock_id {
                return Err("other lock");
            }
        }
        _ => return Err("leg binding missing"),
    }
    if w.nonce == [0; 16] {
        return Err("missing nonce");
    }
    if w.valid_after > w.valid_until {
        return Err("inverted validity window");
    }
    if e.skew_secs > MAX_SKEW_SECS {
        return Err("skew allowance too large");
    }
    // Saturation is exact here: a bound that leaves the u64 range admits every u64 time.
    let earliest = w.valid_after.saturating_sub(e.skew_secs);
    let latest = w.valid_until.saturating_add(e.skew_secs);
    if e.now_real < earliest || e.now_real > latest {
        return Err("outside the validity window");
    }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::caip::{AccountId, AssetId, ChainId};
    use crate::types::{HashAlg, Lock, TimelockSpec};

    const LOCK_ID: Hash32 = [9; 32];

    fn leg() -> Leg {
        Leg {
            chain: ChainId::parse("eip155:1").unwrap(),
            asset: AssetId::parse("eip155:1/slip44:60").unwrap(),
            amount: 1_000_000_000_000_000_000_000_000,
            sender: AccountId::parse(&format!("eip155:1:0x{}", "11".repeat(20))).unwrap(),
            receiver: AccountId::parse(&format!("eip155:1:0x{}", "22".repeat(20))).unwrap(),
            refund_to: AccountId::parse(&format!("eip155:1:0x{}", "11".repeat(20))).unwrap(),
            lock: Lock {
                contract: format!("0x{}", "33".repeat(20)),
                hash_alg: HashAlg::Sha256,
                hashlock: [4; 32],
                preimage_len: 32,
                timelock: TimelockSpec::Time(1_800_000_000),
                swap_id: [1; 32],
                lock_id: LOCK_ID,
                claim_key: None,
                refund_key: None,
            },
        }
    }

    fn warrant() -> SwapWarrant {
        SwapWarrant {
            protocol: PROTOCOL.into(),
            action: Action::Lock,
            swap_id: [1; 32],
            terms_hash: [2; 32],
            inner_sig_hash: [3; 32],
            leg: Some(leg()),
            tx_binding: Some(TxBinding::Evm { signing_hashes: vec![[5; 32]] }),
            facts: vec![FactRecord::new("notional", serde_json::json!(40_000), None)],
            policy_hash: [6; 32],
            policy_version: 3,
            evaluator_id: EvaluatorId::Build([7; 32]),
            decision: "allow".into(),
            reasons: vec![],
            valid_after: 100,
            valid_until: 200,
            nonce: [8; 16],
            prev_warrant: None,
        }
    }

    #[test]
    fn payload_is_canonical_and_stable() {
        let w = warrant();
        let text = String::from_utf8(w.payload().unwrap()).unwrap();
        assert!(text.starts_with(r#"{"action":"lock","decision":"allow","evaluator_id":"0707"#), "{text}");
        assert!(text.contains(r#""amount":"1000000000000000000000000""#), "amounts are decimal strings");
        assert!(text.contains(r#""prev_warrant":null"#));
        assert!(text.contains(r#""tx_binding":{"family":"evm","signing_hashes":["0505"#));
        assert!(text.contains(&format!(r#""lock_id":"{}""#, crate::to_hex(&LOCK_ID))), "the leg carries its lock_id");
        assert!(!text.contains("claim_key") && !text.contains("refund_key"), "Bitcoin keys only on Bitcoin");
        assert!(!text.contains(' '));
        let back: SwapWarrant = serde_json::from_slice(&w.payload().unwrap()).unwrap();
        assert_eq!(back, w);
        assert_eq!(back.hash().unwrap(), w.hash().unwrap());
        let mut other = w.clone();
        other.nonce[0] ^= 1;
        assert_ne!(other.hash().unwrap(), w.hash().unwrap());
    }

    #[test]
    fn accept_warrant_omits_leg() {
        let mut w = warrant();
        w.action = Action::Accept;
        w.leg = None;
        w.tx_binding = None;
        let text = String::from_utf8(w.payload().unwrap()).unwrap();
        assert!(!text.contains("\"leg\"") && !text.contains("tx_binding"));
    }

    #[test]
    fn binding_checks() {
        let w = warrant();
        let chain = ChainId::parse("eip155:1").unwrap();
        let contract = format!("0x{}", "33".repeat(20));
        let ok = Expected { action: Action::Lock, swap_id: &[1; 32], chain: Some(&chain), contract: Some(&contract), lock_id: Some(&LOCK_ID), now_real: 150, skew_secs: 0 };
        assert!(check_binding(&w, &ok).is_ok());
        let other_chain = ChainId::parse("eip155:10").unwrap();
        let other_contract = format!("0x{}", "34".repeat(20));
        let cases = [
            Expected { swap_id: &[9; 32], ..ok },
            Expected { chain: Some(&other_chain), ..ok },
            Expected { contract: Some(&other_contract), ..ok },
            Expected { lock_id: Some(&[1; 32]), ..ok },
            Expected { lock_id: None, ..ok },
            Expected { action: Action::Reveal, ..ok },
            Expected { now_real: 201, ..ok },
            Expected { now_real: 99, ..ok },
        ];
        for (i, e) in cases.iter().enumerate() {
            assert!(check_binding(&w, e).is_err(), "case {i}");
        }
        let mut record_like = w.clone();
        record_like.protocol = DECISION_PROTOCOL.into();
        assert!(check_binding(&record_like, &ok).is_err());
    }

    /// An `accept` warrant (no leg) with the given window, checked at the verifier's
    /// real time with a stated skew.
    fn window_at(valid_after: u64, valid_until: u64, now_real: u64, skew_secs: u64) -> Result<(), &'static str> {
        let mut w = warrant();
        w.action = Action::Accept;
        w.leg = None;
        w.tx_binding = None;
        w.valid_after = valid_after;
        w.valid_until = valid_until;
        let e = Expected { action: Action::Accept, swap_id: &[1; 32], chain: None, contract: None, lock_id: None, now_real, skew_secs };
        check_binding(&w, &e)
    }

    /// An accept warrant has no leg: a verifier that expects a lock never accepts it.
    #[test]
    fn accept_binding_has_no_lock() {
        let mut w = warrant();
        (w.action, w.leg, w.tx_binding) = (Action::Accept, None, None);
        let accept = Expected { action: Action::Accept, swap_id: &[1; 32], chain: None, contract: None, lock_id: None, now_real: 150, skew_secs: 0 };
        assert_eq!(check_binding(&w, &accept), Ok(()));
        assert_eq!(check_binding(&w, &Expected { lock_id: Some(&LOCK_ID), ..accept }), Err("leg binding missing"));
    }

    #[test]
    fn window_uses_real_time_with_stated_skew() {
        for now in [970, 985, 1_000, 1_300, 1_600, 1_615, 1_630] {
            assert_eq!(window_at(1_000, 1_600, now, 30), Ok(()), "now {now}");
        }
        for now in [969, 1_631] {
            assert_eq!(window_at(1_000, 1_600, now, 30), Err("outside the validity window"), "now {now}");
        }
        for now in [999, 1_601] {
            assert_eq!(window_at(1_000, 1_600, now, 0), Err("outside the validity window"), "now {now}");
        }
        assert_eq!(window_at(1_000, 1_600, 1_000, 0), Ok(()));
        assert_eq!(window_at(1_000, 1_600, 1_600, 0), Ok(()));
    }

    #[test]
    fn skew_allowance_is_bounded() {
        let (after, until) = (1_000_000, 1_000_600);
        assert_eq!(window_at(after, until, after - MAX_SKEW_SECS, MAX_SKEW_SECS), Ok(()));
        assert_eq!(window_at(after, until, until + MAX_SKEW_SECS, MAX_SKEW_SECS), Ok(()));
        assert_eq!(window_at(after, until, after - MAX_SKEW_SECS - 1, MAX_SKEW_SECS), Err("outside the validity window"));
        assert_eq!(window_at(after, until, after + 300, MAX_SKEW_SECS + 1), Err("skew allowance too large"));
        assert_eq!(window_at(after, until, after + 300, u64::MAX), Err("skew allowance too large"));
    }

    #[test]
    fn inverted_window_is_rejected() {
        // With the skew, 1_000 and 1_001 are inside [1_001 - 1, 1_000 + 1].
        assert_eq!(window_at(1_001, 1_000, 1_000, 1), Err("inverted validity window"));
        assert_eq!(window_at(1_001, 1_000, 1_001, 1), Err("inverted validity window"));
        // valid_after == valid_until is a valid zero-length window, not an inverted one.
        assert_eq!(window_at(1_000, 1_000, 1_000, 0), Ok(()));
    }

    #[test]
    fn window_bounds_saturate_at_the_u64_range() {
        let k = MAX_SKEW_SECS;
        assert_eq!(window_at(k - 1, u64::MAX, 0, k), Ok(()));
        assert_eq!(window_at(k - 1, u64::MAX, u64::MAX, k), Ok(()));
        assert_eq!(window_at(u64::MAX, u64::MAX, u64::MAX - k, k), Ok(()));
        assert_eq!(window_at(u64::MAX, u64::MAX, u64::MAX - k - 1, k), Err("outside the validity window"));
        assert_eq!(window_at(0, 0, k, k), Ok(()));
        assert_eq!(window_at(0, 0, k + 1, k), Err("outside the validity window"));
    }

    /// One clock for every action (spec 4.1): the same rules hold for a leg action.
    #[test]
    fn window_rules_hold_for_leg_actions() {
        let chain = ChainId::parse("eip155:1").unwrap();
        let contract = format!("0x{}", "33".repeat(20));
        let w = warrant();
        let lock = |now_real, skew_secs| Expected {
            action: Action::Lock,
            swap_id: &[1; 32],
            chain: Some(&chain),
            contract: Some(&contract),
            lock_id: Some(&LOCK_ID),
            now_real,
            skew_secs,
        };
        assert_eq!(check_binding(&w, &lock(70, 30)), Ok(()));
        assert_eq!(check_binding(&w, &lock(230, 30)), Ok(()));
        assert_eq!(check_binding(&w, &lock(69, 30)), Err("outside the validity window"));
        assert_eq!(check_binding(&w, &lock(150, MAX_SKEW_SECS + 1)), Err("skew allowance too large"));
        let mut inverted = w.clone();
        (inverted.valid_after, inverted.valid_until) = (201, 200);
        assert_eq!(check_binding(&inverted, &lock(200, 1)), Err("inverted validity window"));
    }

    #[test]
    fn evaluator_id_encoding() {
        assert_eq!(serde_json::to_string(&EvaluatorId::HumanReview).unwrap(), "\"human-review\"");
        let id: EvaluatorId = serde_json::from_str(&format!("\"{}\"", crate::to_hex(&[7; 32]))).unwrap();
        assert_eq!(id, EvaluatorId::Build([7; 32]));
        assert!(serde_json::from_str::<EvaluatorId>("\"robot\"").is_err());
    }
}
