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

/// What a verifier expects a warrant to authorize.
#[derive(Clone, Copy)]
pub struct Expected<'a> {
    pub action: Action,
    pub swap_id: &'a Hash32,
    pub chain: Option<&'a crate::caip::ChainId>,
    pub contract: Option<&'a str>,
    /// Chain time of the leg chain (or the signer's time for `accept`).
    pub now: u64,
}

/// S20: the warrant binds chain id, contract, swap id, nonce and validity window,
/// and it is an authorization, not a decision record.
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
    match (&w.leg, e.chain, e.contract) {
        (None, None, None) => {}
        (Some(leg), Some(chain), Some(contract)) => {
            if &leg.chain != chain {
                return Err("other chain");
            }
            if leg.lock.contract != contract {
                return Err("other contract");
            }
            if &leg.lock.swap_id != e.swap_id {
                return Err("lock of another swap");
            }
        }
        _ => return Err("leg binding missing"),
    }
    if w.nonce == [0; 16] {
        return Err("missing nonce");
    }
    if e.now < w.valid_after || e.now > w.valid_until {
        return Err("outside the validity window");
    }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::caip::{AccountId, AssetId, ChainId};
    use crate::types::{HashAlg, Lock, TimelockSpec};

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
                keys: None,
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
        let ok = Expected { action: Action::Lock, swap_id: &[1; 32], chain: Some(&chain), contract: Some(&contract), now: 150 };
        assert!(check_binding(&w, &ok).is_ok());
        let other_chain = ChainId::parse("eip155:10").unwrap();
        let other_contract = format!("0x{}", "34".repeat(20));
        let cases = [
            Expected { swap_id: &[9; 32], ..ok },
            Expected { chain: Some(&other_chain), ..ok },
            Expected { contract: Some(&other_contract), ..ok },
            Expected { action: Action::Reveal, ..ok },
            Expected { now: 201, ..ok },
            Expected { now: 99, ..ok },
        ];
        for (i, e) in cases.iter().enumerate() {
            assert!(check_binding(&w, e).is_err(), "case {i}");
        }
        let mut record_like = w.clone();
        record_like.protocol = DECISION_PROTOCOL.into();
        assert!(check_binding(&record_like, &ok).is_err());
    }

    #[test]
    fn evaluator_id_encoding() {
        assert_eq!(serde_json::to_string(&EvaluatorId::HumanReview).unwrap(), "\"human-review\"");
        let id: EvaluatorId = serde_json::from_str(&format!("\"{}\"", crate::to_hex(&[7; 32]))).unwrap();
        assert_eq!(id, EvaluatorId::Build([7; 32]));
        assert!(serde_json::from_str::<EvaluatorId>("\"robot\"").is_err());
    }
}
