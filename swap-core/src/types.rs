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
    /// The absolute timelock; `None` for a relative timelock before confirmation.
    pub fn absolute(&self, confirmed_height: Option<u64>, confirmed_time: Option<u64>) -> Option<Timelock> {
        match *self {
            TimelockSpec::Height(h) => Some(Timelock::Height(h)),
            TimelockSpec::Time(t) => Some(Timelock::Time(t)),
            TimelockSpec::RelativeBlocks(n) => Some(Timelock::Height(confirmed_height?.checked_add(n)?)),
            TimelockSpec::RelativeSeconds(n) => Some(Timelock::Time(confirmed_time?.checked_add(n)?)),
        }
    }
}

/// X-only public keys of a Bitcoin Taproot HTLC.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HtlcKeys {
    #[serde(with = "enc::hex32")]
    pub receiver: Hash32,
    #[serde(with = "enc::hex32")]
    pub refund: Hash32,
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
    /// Required on Bitcoin, absent elsewhere.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keys: Option<HtlcKeys>,
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

impl Leg {
    /// The first height or chain time at which the refund is valid: the input of
    /// the verified clock arithmetic. Bitcoin `OP_CHECKLOCKTIMEVERIFY` with operand
    /// `h` needs `nLockTime ≥ h`, and a transaction with `nLockTime = h` is final
    /// only in a block above `h` (for times: once the median time past exceeds
    /// `t`). The reference EVM and Solana HTLCs refund once the block time is at
    /// least `t`. `None` for a relative timelock before the lock confirms.
    pub fn refund_valid_from(&self) -> Option<Timelock> {
        let t = self.lock.timelock.absolute(None, None)?;
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
        assert_eq!(TimelockSpec::RelativeBlocks(144).absolute(Some(800_000), None), Some(Timelock::Height(800_144)));
        assert_eq!(TimelockSpec::RelativeBlocks(144).absolute(None, None), None);
        assert_eq!(TimelockSpec::Time(5).absolute(None, None), Some(Timelock::Time(5)));
        assert_eq!(TimelockSpec::RelativeSeconds(u64::MAX).absolute(None, Some(1)), None);
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

    #[test]
    fn risk_flag_names() {
        for f in RiskFlag::ALL {
            assert_eq!(RiskFlag::from_name(f.name()), Some(f));
            assert_eq!(serde_json::to_string(&f).unwrap(), format!("\"{}\"", f.name()));
        }
        assert_eq!(RiskFlag::mask(&[RiskFlag::FreezableByIssuer, RiskFlag::TransferFee]), 0b1_0001);
    }
}
