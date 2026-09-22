//! Fixed interpreter, owner-approved policy data, and authenticated task evidence.
//! No natural-language interpretation or model verdict is trusted here.

use k256::ecdsa::{signature::Verifier, Signature, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
pub use warrant_verified_policy::Rule;
use warrant_verified_policy::{evaluate, Facts};

pub type Hash = [u8; 32];
pub type Address = [u8; 20];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scope {
    pub chain_id: u64,
    pub vault: Address,
    pub token: Address,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub version: u64,
    pub scope: Scope,
    pub valid_after: u64,
    pub valid_until: u64,
    /// SEC1 compressed secp256k1 keys: policy owner approves these authorities.
    pub registry_key: Vec<u8>,
    pub acceptance_key: Vec<u8>,
    pub rule: Rule,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VendorCredential {
    pub scope: Scope,
    pub recipient: Address,
    pub category: u16,
    pub valid_after: u64,
    pub valid_until: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Acceptance {
    pub scope: Scope,
    /// A stable obligation identifier assigned by the acceptance authority.
    pub task_id: Hash,
    pub deliverable_hash: Hash,
    pub recipient: Address,
    pub amount: u64,
    pub accepted: bool,
    pub valid_after: u64,
    pub valid_until: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub scope: Scope,
    pub task_id: Hash,
    pub deliverable_hash: Hash,
    pub recipient: Address,
    pub amount: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub vendor: VendorCredential,
    pub vendor_signature: Vec<u8>,
    pub acceptance: Acceptance,
    pub acceptance_signature: Vec<u8>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub policy: Policy,
    pub request: Request,
    pub evidence: Evidence,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Authorization {
    pub policy_hash: Hash,
    pub request: Request,
    pub policy_version: u64,
    pub valid_after: u64,
    pub valid_until: u64,
    pub evidence_hash: Hash,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Denial {
    InvalidPolicy,
    InvalidRequest,
    InvalidEvidence,
    InvalidSignature,
    ScopeMismatch,
    RequestMismatch,
    EmptyValidityWindow,
    PolicyDenied,
}

impl std::fmt::Display for Denial {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Denial {}

// Canonical encodings use only typed structs, arrays, integers and ordered lists.
// No JSON object order, whitespace, floats, or map iteration affects commitments.
pub fn tagged_bytes<T: Serialize>(tag: &[u8], value: &T) -> Vec<u8> {
    let encoded = bincode::serialize(value).expect("typed serialization");
    let mut bytes = Vec::with_capacity(tag.len() + encoded.len() + 16);
    bytes.extend_from_slice(&(tag.len() as u64).to_le_bytes());
    bytes.extend_from_slice(tag);
    bytes.extend_from_slice(&(encoded.len() as u64).to_le_bytes());
    bytes.extend_from_slice(&encoded);
    bytes
}

pub fn hash_tagged<T: Serialize>(tag: &[u8], value: &T) -> Hash {
    Sha256::digest(tagged_bytes(tag, value)).into()
}

pub fn policy_hash(policy: &Policy) -> Hash {
    hash_tagged(b"warrant/policy/v1", policy)
}

pub fn vendor_message(credential: &VendorCredential) -> Vec<u8> {
    tagged_bytes(b"warrant/vendor/v1", credential)
}

pub fn acceptance_message(acceptance: &Acceptance) -> Vec<u8> {
    tagged_bytes(b"warrant/acceptance/v1", acceptance)
}

fn valid_scope(scope: &Scope) -> bool {
    scope.chain_id != 0 && scope.vault != [0; 20] && scope.token != [0; 20]
}

fn valid_rule(rule: &Rule, depth: usize, remaining: &mut usize) -> bool {
    if depth > 8 || *remaining == 0 {
        return false;
    }
    *remaining -= 1;
    match rule {
        Rule::All(children) | Rule::Any(children) => {
            !children.is_empty()
                && children.len() <= 16
                && children.iter().all(|r| valid_rule(r, depth + 1, remaining))
        }
        Rule::AmountAtMost(cap) => *cap > 0,
        Rule::VendorCategoryIn(categories) => {
            !categories.is_empty() && categories.len() <= 64 && !categories.contains(&0)
        }
        Rule::Accepted => true,
        Rule::DeliverableEquals(hash) => *hash != [0; 32],
        Rule::RecipientEquals(address) => *address != [0; 20],
    }
}

fn valid_key(bytes: &[u8]) -> bool {
    bytes.len() == 33 && VerifyingKey::from_sec1_bytes(bytes).is_ok()
}

pub fn validate_policy(policy: &Policy) -> Result<(), Denial> {
    if policy.version == 0
        || !valid_scope(&policy.scope)
        || policy.valid_after > policy.valid_until
        || !valid_key(&policy.registry_key)
        || !valid_key(&policy.acceptance_key)
        || !valid_rule(&policy.rule, 0, &mut 128)
    {
        return Err(Denial::InvalidPolicy);
    }
    Ok(())
}

fn verify(key: &[u8], message: &[u8], signature: &[u8]) -> Result<(), Denial> {
    let key = VerifyingKey::from_sec1_bytes(key).map_err(|_| Denial::InvalidSignature)?;
    let sig = Signature::from_slice(signature).map_err(|_| Denial::InvalidSignature)?;
    // Use canonical low-S signatures to avoid alternate encodings of evidence.
    if sig.normalize_s().is_some() {
        return Err(Denial::InvalidSignature);
    }
    key.verify(message, &sig)
        .map_err(|_| Denial::InvalidSignature)
}

fn matches(rule: &Rule, request: &Request, evidence: &Evidence) -> bool {
    // Authentication and request/evidence binding happen in authorize() first.
    // This projection is integration code, outside the evaluator's Verus proof.
    evaluate(
        rule,
        &Facts {
            amount: request.amount,
            category: evidence.vendor.category,
            accepted: evidence.acceptance.accepted,
            deliverable: request.deliverable_hash,
            recipient: request.recipient,
        },
    )
}

pub fn authorize(input: &Input) -> Result<Authorization, Denial> {
    let Input {
        policy,
        request,
        evidence,
    } = input;
    validate_policy(policy)?;
    if request.amount == 0
        || request.recipient == [0; 20]
        || request.task_id == [0; 32]
        || request.deliverable_hash == [0; 32]
    {
        return Err(Denial::InvalidRequest);
    }

    let vendor = &evidence.vendor;
    let acceptance = &evidence.acceptance;
    if request.scope != policy.scope
        || vendor.scope != policy.scope
        || acceptance.scope != policy.scope
    {
        return Err(Denial::ScopeMismatch);
    }
    if vendor.category == 0
        || vendor.valid_after > vendor.valid_until
        || acceptance.valid_after > acceptance.valid_until
    {
        return Err(Denial::InvalidEvidence);
    }
    if vendor.recipient != request.recipient
        || acceptance.recipient != request.recipient
        || acceptance.task_id != request.task_id
        || acceptance.deliverable_hash != request.deliverable_hash
        || acceptance.amount != request.amount
    {
        return Err(Denial::RequestMismatch);
    }

    verify(
        &policy.registry_key,
        &vendor_message(vendor),
        &evidence.vendor_signature,
    )?;
    verify(
        &policy.acceptance_key,
        &acceptance_message(acceptance),
        &evidence.acceptance_signature,
    )?;

    let valid_after = policy
        .valid_after
        .max(vendor.valid_after)
        .max(acceptance.valid_after);
    let valid_until = policy
        .valid_until
        .min(vendor.valid_until)
        .min(acceptance.valid_until);
    if valid_after > valid_until {
        return Err(Denial::EmptyValidityWindow);
    }
    if !matches(&policy.rule, request, evidence) {
        return Err(Denial::PolicyDenied);
    }

    Ok(Authorization {
        policy_hash: policy_hash(policy),
        request: request.clone(),
        policy_version: policy.version,
        valid_after,
        valid_until,
        evidence_hash: hash_tagged(b"warrant/evidence/v1", evidence),
    })
}

impl Authorization {
    /// Solidity ABI: 12 static 32-byte words. The guest commits these exact bytes.
    pub fn journal(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(384);
        fn integer(out: &mut Vec<u8>, n: u64) {
            out.extend_from_slice(&[0; 24]);
            out.extend_from_slice(&n.to_be_bytes());
        }
        fn address(out: &mut Vec<u8>, a: &Address) {
            out.extend_from_slice(&[0; 12]);
            out.extend_from_slice(a);
        }
        out.extend_from_slice(&self.policy_hash);
        integer(&mut out, self.request.scope.chain_id);
        address(&mut out, &self.request.scope.vault);
        address(&mut out, &self.request.scope.token);
        address(&mut out, &self.request.recipient);
        integer(&mut out, self.request.amount);
        out.extend_from_slice(&self.request.task_id);
        out.extend_from_slice(&self.request.deliverable_hash);
        integer(&mut out, self.policy_version);
        integer(&mut out, self.valid_after);
        integer(&mut out, self.valid_until);
        out.extend_from_slice(&self.evidence_hash);
        out
    }
}
