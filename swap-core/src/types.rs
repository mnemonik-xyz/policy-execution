//! Legs, locks, terms and actions (spec sections 3.2 and 3.3).

use crate::caip::{AccountId, AssetId, ChainId};
use crate::enc;
use crate::verified::Timelock;
use crate::Hash32;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HashAlg {
    Sha256,
}

/// A timelock as negotiated. Relative timelocks count from the confirmation of
/// the lock and become absolute once the lock is observed.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum TimelockSpec {
    Height(u64),
    Time(u64),
    RelativeBlocks(u64),
    RelativeSeconds(u64),
}

impl TimelockSpec {
    /// Whether the timelock counts from the confirmation of the lock.
    pub fn is_relative(&self) -> bool {
        matches!(self, TimelockSpec::RelativeBlocks(_) | TimelockSpec::RelativeSeconds(_))
    }

    /// The absolute timelock. A relative block count needs the height of the block
    /// that confirms the lock: BIP 68 permits a spend with a relative lock of `n`
    /// blocks first in that block plus `n`. A relative time is always `None`: BIP 68
    /// counts it from the median time past of the block before the confirming
    /// block, in units of 512 seconds, which no observation carries, and no profile
    /// accepts it (`checks::timelock_form`).
    pub fn absolute(&self, confirmed_height: Option<u64>) -> Option<Timelock> {
        match *self {
            TimelockSpec::Height(h) => Some(Timelock::Height(h)),
            TimelockSpec::Time(t) => Some(Timelock::Time(t)),
            TimelockSpec::RelativeBlocks(n) => Some(Timelock::Height(confirmed_height?.checked_add(n)?)),
            TimelockSpec::RelativeSeconds(_) => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lock {
    /// Contract identity: a script template id, a contract address or a program id.
    pub contract: String,
    pub hash_alg: HashAlg,
    #[serde(with = "enc::hex32")]
    pub hashlock: Hash32,
    pub preimage_len: u32,
    pub timelock: TimelockSpec,
    #[serde(with = "enc::hex32")]
    pub swap_id: Hash32,
    /// The key of this lock on its chain: `sha256(swap_id ‖ leg ‖ sender)` (spec 3.2,
    /// `lock_id`). S10 checks it against the derivation (`Leg::derived_lock_id`).
    #[serde(with = "enc::hex32")]
    pub lock_id: Hash32,
    /// Bitcoin only: the 32-byte x-only key of the claim leaf (BIP 340).
    #[serde(default, skip_serializing_if = "Option::is_none", with = "enc::hex32_opt")]
    pub claim_key: Option<Hash32>,
    /// Bitcoin only: the 32-byte x-only key of the refund leaf (BIP 340).
    #[serde(default, skip_serializing_if = "Option::is_none", with = "enc::hex32_opt")]
    pub refund_key: Option<Hash32>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Leg {
    pub chain: ChainId,
    pub asset: AssetId,
    /// Base units of the asset; a decimal string in JSON.
    #[serde(with = "enc::amount")]
    pub amount: u128,
    pub sender: AccountId,
    pub receiver: AccountId,
    pub refund_to: AccountId,
    pub lock: Lock,
}

/// `lock_id = sha256(swap_id ‖ leg ‖ sender)` (spec 3.2). `leg` is one byte
/// (`LegName::lock_byte`); `sender` is the chain-native bytes that the lock sees.
pub fn lock_id(swap_id: &Hash32, leg: LegName, sender: &[u8]) -> Hash32 {
    let mut data = Vec::with_capacity(33 + sender.len());
    data.extend_from_slice(swap_id);
    data.push(leg.lock_byte());
    data.extend_from_slice(sender);
    crate::sha256(&data)
}

impl Leg {
    /// The funding sender as the lock sees it (spec 3.2; the byte form of each
    /// profile, implementation spec 4.5). EVM: the 20-byte address that calls
    /// `lock` (`msg.sender`). Solana: the 32-byte key that signs the lock
    /// instruction. Bitcoin: the 32-byte x-only `refund_key`, the funder's key in the
    /// lock; a Taproot output sees no sender. `None` when the leg lacks it.
    pub fn sender_bytes(&self) -> Option<Vec<u8>> {
        match self.chain.family()? {
            crate::caip::Family::Evm => self.sender.evm_address().map(|a| a.to_vec()),
            crate::caip::Family::Solana => self.sender.solana_key().map(|k| k.to_vec()),
            crate::caip::Family::Bitcoin => self.lock.refund_key.map(|k| k.to_vec()),
        }
    }

    /// The `lock_id` that the lock of this leg must carry, as leg `which`.
    pub fn derived_lock_id(&self, which: LegName) -> Option<Hash32> {
        Some(lock_id(&self.lock.swap_id, which, &self.sender_bytes()?))
    }

    /// The first height or chain time at which the refund is valid: the input of
    /// the verified clock arithmetic. Bitcoin `OP_CHECKLOCKTIMEVERIFY` with operand
    /// `h` needs `nLockTime ≥ h`, and a transaction with `nLockTime = h` is final
    /// only in a block above `h` (for times: once the median time past exceeds
    /// `t`). The reference EVM and Solana HTLCs refund once the block time is at
    /// least `t`. `None` for a relative timelock before the lock confirms.
    pub fn refund_valid_from(&self) -> Option<Timelock> {
        let t = self.lock.timelock.absolute(None)?;
        if self.chain.family() == Some(crate::caip::Family::Bitcoin) {
            if let TimelockSpec::Height(_) | TimelockSpec::Time(_) = self.lock.timelock {
                return Some(match t {
                    Timelock::Height(h) => Timelock::Height(h.checked_add(1)?),
                    Timelock::Time(t) => Timelock::Time(t.checked_add(1)?),
                });
            }
        }
        Some(t)
    }

    /// As `refund_valid_from`, for a lock in block `confirmed_height`: a relative
    /// block count becomes absolute (spec 7.3). BIP 68 needs no CLTV adjustment.
    pub fn refund_valid_from_confirmed(&self, confirmed_height: u64) -> Option<Timelock> {
        match self.lock.timelock {
            TimelockSpec::RelativeBlocks(_) => self.lock.timelock.absolute(Some(confirmed_height)),
            _ => self.refund_valid_from(),
        }
    }
}

/// The agreed terms that both parties sign in the ACCEPT message.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Terms {
    #[serde(with = "enc::hex32")]
    pub swap_id: Hash32,
    /// Identity of the initiator (for example a `did:key`).
    pub initiator: String,
    pub responder: String,
    /// The initiator's asset; the initiator funds it and locks first.
    pub leg_a: Leg,
    /// The responder's asset.
    pub leg_b: Leg,
}

impl Terms {
    pub fn hash(&self) -> Result<Hash32, crate::jcs::JcsError> {
        Ok(crate::blake3(&crate::jcs::to_vec(self)?))
    }

    pub fn leg(&self, which: LegName) -> &Leg {
        match which {
            LegName::A => &self.leg_a,
            LegName::B => &self.leg_b,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LegName {
    A,
    B,
}

impl LegName {
    /// The byte of the leg in `lock_id` (spec 3.2): `0x41` for leg A, `0x42` for leg B.
    pub fn lock_byte(self) -> u8 {
        match self {
            LegName::A => 0x41,
            LegName::B => 0x42,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Initiator,
    Responder,
}

impl Role {
    /// The leg that this party funds.
    pub fn own_leg(self) -> LegName {
        match self {
            Role::Initiator => LegName::A,
            Role::Responder => LegName::B,
        }
    }

    pub fn counterparty_leg(self) -> LegName {
        match self {
            Role::Initiator => LegName::B,
            Role::Responder => LegName::A,
        }
    }

    pub fn counterparty(self, terms: &Terms) -> &str {
        match self {
            Role::Initiator => &terms.responder,
            Role::Responder => &terms.initiator,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Accept,
    Lock,
    Reveal,
    Claim,
    Refund,
}

impl Action {
    /// Exit actions recover value; no policy may block them (spec 3.3, S18).
    pub fn is_exit(self) -> bool {
        matches!(self, Action::Claim | Action::Refund)
    }

    /// The leg that the action touches; `None` for `accept`.
    pub fn leg(self, role: Role) -> Option<LegName> {
        match self {
            Action::Accept => None,
            Action::Lock | Action::Refund => Some(role.own_leg()),
            Action::Reveal | Action::Claim => Some(role.counterparty_leg()),
        }
    }

    /// Who may take the action (spec 3.3).
    pub fn allowed_for(self, role: Role) -> bool {
        match self {
            Action::Accept | Action::Lock | Action::Refund => true,
            Action::Reveal => role == Role::Initiator,
            Action::Claim => role == Role::Responder,
        }
    }
}

/// Universal asset risk flags (spec 8.5), as bits of the evaluator's `asset_risk`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RiskFlag {
    FreezableByIssuer,
    SeizableByIssuer,
    Pausable,
    Upgradeable,
    TransferFee,
    TransferHook,
    Rebasing,
    ConfidentialAmount,
    NonTransferable,
}

impl RiskFlag {
    pub const ALL: [RiskFlag; 9] = [
        RiskFlag::FreezableByIssuer,
        RiskFlag::SeizableByIssuer,
        RiskFlag::Pausable,
        RiskFlag::Upgradeable,
        RiskFlag::TransferFee,
        RiskFlag::TransferHook,
        RiskFlag::Rebasing,
        RiskFlag::ConfidentialAmount,
        RiskFlag::NonTransferable,
    ];

    pub fn bit(self) -> u32 {
        1 << (self as u32)
    }

    /// Flags that always deny: the signer cannot verify or complete the swap.
    pub fn always_denied(self) -> bool {
        matches!(self, RiskFlag::ConfidentialAmount | RiskFlag::NonTransferable)
    }

    pub fn mask(flags: &[RiskFlag]) -> u32 {
        flags.iter().fold(0, |m, f| m | f.bit())
    }

    pub fn name(self) -> &'static str {
        match self {
            RiskFlag::FreezableByIssuer => "freezable_by_issuer",
            RiskFlag::SeizableByIssuer => "seizable_by_issuer",
            RiskFlag::Pausable => "pausable",
            RiskFlag::Upgradeable => "upgradeable",
            RiskFlag::TransferFee => "transfer_fee",
            RiskFlag::TransferHook => "transfer_hook",
            RiskFlag::Rebasing => "rebasing",
            RiskFlag::ConfidentialAmount => "confidential_amount",
            RiskFlag::NonTransferable => "non_transferable",
        }
    }

    pub fn from_name(name: &str) -> Option<RiskFlag> {
        RiskFlag::ALL.into_iter().find(|f| f.name() == name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relative_timelocks_become_absolute() {
        assert_eq!(TimelockSpec::RelativeBlocks(144).absolute(Some(800_000)), Some(Timelock::Height(800_144)));
        assert_eq!(TimelockSpec::RelativeBlocks(144).absolute(None), None);
        assert_eq!(TimelockSpec::RelativeBlocks(u64::MAX).absolute(Some(1)), None);
        assert_eq!(TimelockSpec::Time(5).absolute(None), Some(Timelock::Time(5)));
        // No observation carries the BIP 68 time base: a relative time stays unknown.
        assert_eq!(TimelockSpec::RelativeSeconds(3_600).absolute(Some(800_000)), None);
        assert!(TimelockSpec::RelativeBlocks(1).is_relative() && TimelockSpec::RelativeSeconds(1).is_relative());
        assert!(!TimelockSpec::Height(1).is_relative() && !TimelockSpec::Time(1).is_relative());
    }

    #[test]
    fn actions_and_roles() {
        assert!(Action::Claim.is_exit() && Action::Refund.is_exit() && !Action::Reveal.is_exit());
        assert_eq!(Action::Reveal.leg(Role::Initiator), Some(LegName::B));
        assert_eq!(Action::Claim.leg(Role::Responder), Some(LegName::A));
        assert_eq!(Action::Lock.leg(Role::Responder), Some(LegName::B));
        assert!(!Action::Reveal.allowed_for(Role::Responder));
        assert!(!Action::Claim.allowed_for(Role::Initiator));
    }

    #[test]
    fn other_hash_functions_do_not_parse() {
        assert!(serde_json::from_str::<HashAlg>("\"sha256\"").is_ok());
        for other in ["keccak256", "sha3_256", "ripemd160", "SHA256"] {
            assert!(serde_json::from_str::<HashAlg>(&format!("\"{other}\"")).is_err(), "{other}");
        }
    }

    /// Spec 3.2: `lock_id`, `claim_key` and `refund_key` are Lock fields. The
    /// version 0.2 `keys` object and a lock without `lock_id` do not parse.
    #[test]
    fn lock_fields_and_lock_id() {
        let lock = |extra: serde_json::Value| {
            let mut v = serde_json::json!({ "contract": "c", "hash_alg": "sha256", "hashlock": "11".repeat(32), "preimage_len": 32,
                                            "timelock": {"kind": "height", "value": 1}, "swap_id": "51".repeat(32) });
            v.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
            serde_json::from_value::<Lock>(v)
        };
        let l = lock(serde_json::json!({ "lock_id": "77".repeat(32), "claim_key": "0a".repeat(32), "refund_key": "0b".repeat(32) })).unwrap();
        assert_eq!((l.lock_id, l.claim_key, l.refund_key), ([0x77; 32], Some([0x0a; 32]), Some([0x0b; 32])));
        let text = serde_json::to_string(&l).unwrap();
        assert!(text.contains(r#""claim_key":"0a0a"#) && text.contains(r#""refund_key":"0b0b"#), "{text}");
        let plain = lock(serde_json::json!({ "lock_id": "77".repeat(32) })).unwrap();
        assert!(!serde_json::to_string(&plain).unwrap().contains("_key"), "absent keys are not serialized");
        assert!(lock(serde_json::json!({})).is_err(), "lock_id is required");
        let old = serde_json::json!({ "lock_id": "77".repeat(32), "keys": { "receiver": "0a".repeat(32), "refund": "0b".repeat(32) } });
        assert!(lock(old).is_err(), "the version 0.2 keys object");
        // lock_id = sha256(swap_id ‖ leg ‖ sender), leg byte 0x41 or 0x42.
        assert_eq!(
            crate::to_hex(&lock_id(&[0x51; 32], LegName::B, &[0xaa; 20])),
            "960cb8911e96f8420f9db1bf0851469a93442911d3620cd2da7e919324a5c6b8"
        );
        assert_eq!((LegName::A.lock_byte(), LegName::B.lock_byte()), (b'A', b'B'));
        // The sender bytes of each family (Python hashlib vectors, swap_id 0x51 x 32).
        let leg = |account: &str, refund_key: Option<Hash32>| -> Leg {
            let chain = account.rsplit_once(':').unwrap().0;
            serde_json::from_value(serde_json::json!({
                "chain": chain, "asset": format!("{chain}/slip44:0"), "amount": "1",
                "sender": account, "receiver": account, "refund_to": account,
                "lock": { "contract": "c", "hash_alg": "sha256", "hashlock": "11".repeat(32), "preimage_len": 32,
                          "timelock": {"kind": "time", "value": 1}, "swap_id": "51".repeat(32), "lock_id": "00".repeat(32),
                          "refund_key": refund_key.map(|k| crate::to_hex(&k)) }
            }))
            .unwrap()
        };
        let hex = |h: Option<Hash32>| h.map(|h| crate::to_hex(&h));
        let evm = leg(&format!("eip155:1:0x{}", "aa".repeat(20)), None);
        assert_eq!(hex(evm.derived_lock_id(LegName::A)).as_deref(), Some("8566ed2b109335330d2ff5e10291adcf1c9f1624705a2b43e659a6671ea9665c"));
        let sol = leg(&format!("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp:{}", bs58::encode([0xbb; 32]).into_string()), None);
        assert_eq!(hex(sol.derived_lock_id(LegName::B)).as_deref(), Some("31cc5ec933cbf8bde2056ef1a19d3f191f32d0ac5ea2a1de5bffefa385681533"));
        // Bitcoin: the refund_key, never the CAIP-10 sender, which no output sees.
        let btc = leg("bip122:000000000019d6689c085ae165831e93:bc1pa", Some([0x10; 32]));
        assert_eq!(hex(btc.derived_lock_id(LegName::A)).as_deref(), Some("dfacec4245477865a290de7b6f73f45c21b91d3169ed0b005bfa33f733a47904"));
        let other = leg("bip122:000000000019d6689c085ae165831e93:bc1pb", Some([0x10; 32]));
        assert_eq!(other.derived_lock_id(LegName::A), btc.derived_lock_id(LegName::A));
        assert_eq!(leg("bip122:000000000019d6689c085ae165831e93:bc1pa", None).derived_lock_id(LegName::A), None);
    }

    #[test]
    fn risk_flag_names() {
        for f in RiskFlag::ALL {
            assert_eq!(RiskFlag::from_name(f.name()), Some(f));
            assert_eq!(serde_json::to_string(&f).unwrap(), format!("\"{}\"", f.name()));
        }
        assert_eq!(RiskFlag::mask(&[RiskFlag::FreezableByIssuer, RiskFlag::TransferFee]), 0b1_0001);
    }
}
