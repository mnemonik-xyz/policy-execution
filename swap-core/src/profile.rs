//! Chain profiles (spec section 8). A profile is a small, reviewed adapter. The
//! owner pins its hash in the policy. A chain without every obligatory item is
//! not supported (spec 8.8).

use crate::caip::{ChainId, Family};
use crate::facts::EvidenceMethod;
use crate::verified::ClockBounds;
use crate::Hash32;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeeRaise {
    /// Bitcoin: replace-by-fee or child-pays-for-parent.
    RbfOrCpfp,
    /// EVM: replace with the same nonce and a higher fee.
    SameNonceReplacement,
    /// Solana: resubmit with a new blockhash and a higher priority fee.
    Resubmit,
    None,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RefundMethod {
    /// The signer signs the refund at lock time.
    Prepared,
    /// Anyone can trigger the refund to the fixed `refund_to`.
    Permissionless,
    None,
}

/// Finality and minimum evidence for one value band (notional in whole units of
/// the reference currency, inclusive upper bound).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValueBand {
    pub max_notional: u64,
    /// Confirmations; ignored when `finalized_tag` is set.
    pub confirmations: u64,
    /// Require the chain's finalized status instead of a depth.
    pub finalized_tag: bool,
    pub min_evidence: EvidenceMethod,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fees {
    /// Worst-case fee of a lock, a claim and a refund, in base units of the native
    /// coin. On Bitcoin the decoder rejects a transaction that pays more.
    #[serde(with = "crate::enc::amount")]
    pub worst_lock: u128,
    #[serde(with = "crate::enc::amount")]
    pub worst_claim: u128,
    #[serde(with = "crate::enc::amount")]
    pub worst_refund: u128,
    pub raise: FeeRaise,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChainProfile {
    pub chain: ChainId,
    pub mainnet: bool,
    pub family: Family,
    pub clock: ClockBounds,
    /// Time to see a revealed secret on this chain (S11 `D_observe`).
    pub d_observe_secs: u64,
    /// Time to get a claim on this chain to finality at the worst-case fee (S11, S12).
    pub d_confirm_secs: u64,
    /// Time for a refund on this chain to become final after `T` at the worst-case
    /// fee (S11 `D_refund`). It counts while a claim stays valid after `T`.
    pub d_refund_secs: u64,
    /// The lock rejects a claim after `T`; then S11 needs no `D_refund`. Never
    /// true on Bitcoin, where script has no "before T" check.
    pub claim_closes_at_timelock: bool,
    /// The failure probability at which the clock bounds hold, for example "1e-6".
    pub clock_failure_probability: String,
    /// Bands in increasing order; a notional above the last band is not supported.
    pub value_bands: Vec<ValueBand>,
    pub fees: Fees,
    pub refund: RefundMethod,
    /// The lock template keys each lock by `lock_id` (spec 8.1, S10): the Bitcoin
    /// claim leaf commits to it, the EVM contract stores the lock under it, the
    /// Solana escrow PDA has it as a seed. True for the three reference families.
    pub lock_id_binding: bool,
    pub template_enforces_len32: bool,
    /// Source and date of the measured clock bounds (spec 8.8 item 3).
    pub clock_source: String,
}

impl ChainProfile {
    pub fn hash(&self) -> Hash32 {
        crate::blake3(&crate::jcs::to_vec(self).expect("profile has no floats"))
    }

    /// S11 `D_refund`: 0 only when the lock rejects a claim after `T`.
    pub fn d_refund(&self) -> u64 {
        if self.claim_closes_at_timelock { 0 } else { self.d_refund_secs }
    }

    /// Obligatory items that the profile lacks (spec 8.1). Empty means supported.
    pub fn missing_obligatory(&self) -> Vec<&'static str> {
        let mut missing = Vec::new();
        if self.chain.family() != Some(self.family) {
            missing.push("family");
        }
        if !self.template_enforces_len32 {
            missing.push("lock template with a 32-byte preimage check");
        }
        let c = &self.clock;
        let bitcoin = self.family == Family::Bitcoin;
        // A Bitcoin time lock reads the median time past of 11 blocks (BIP 113).
        if c.fast_block_secs > c.slow_block_secs || c.slow_block_secs == 0 || (bitcoin && c.time_settle_blocks < 6) {
            missing.push("clock bounds");
        }
        if self.clock_source.trim().is_empty() || self.clock_failure_probability.trim().is_empty() {
            missing.push("clock bound source");
        }
        if (bitcoin && self.claim_closes_at_timelock) || (!self.claim_closes_at_timelock && self.d_refund_secs == 0) {
            missing.push("refund finality time");
        }
        if self.value_bands.is_empty()
            || self.value_bands.windows(2).any(|w| w[0].max_notional >= w[1].max_notional)
        {
            missing.push("finality rule");
        }
        if self.fees.raise == FeeRaise::None {
            missing.push("fee-raising method");
        }
        if self.refund == RefundMethod::None {
            missing.push("prepared or permissionless refund");
        }
        if self.d_confirm_secs == 0 {
            missing.push("confirmation time");
        }
        missing
    }

    /// The finality and evidence requirement for a notional; `None` above the last band.
    pub fn band(&self, notional: u64) -> Option<&ValueBand> {
        self.value_bands.iter().find(|b| notional <= b.max_notional)
    }
}

/// The profiles that a policy pins, by chain.
#[derive(Clone, Debug, Default)]
pub struct ProfileSet {
    profiles: Vec<ChainProfile>,
}

impl ProfileSet {
    pub fn new(profiles: Vec<ChainProfile>) -> Self {
        ProfileSet { profiles }
    }

    pub fn get(&self, chain: &ChainId) -> Option<&ChainProfile> {
        self.profiles.iter().find(|p| &p.chain == chain)
    }
}

/// Reference profiles for tests and local development. Production profiles carry
/// measured clock bounds with a source and a date (spec 8.8).
pub mod reference {
    use super::*;

    pub const BITCOIN_MAINNET: &str = "bip122:000000000019d6689c085ae165831e93";
    pub const BITCOIN_REGTEST: &str = "bip122:0f9188f13cb7b2c71f2a335e3a4fc328";
    pub const ETHEREUM_MAINNET: &str = "eip155:1";
    pub const SOLANA_MAINNET: &str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";

    pub fn bitcoin(chain: &str) -> ChainProfile {
        ChainProfile {
            chain: ChainId::parse(chain).unwrap(),
            mainnet: chain == BITCOIN_MAINNET,
            family: Family::Bitcoin,
            // The real time of n blocks: at least 400 n - 20 000 s, at most 900 n + 20 000 s.
            clock: ClockBounds {
                fast_block_secs: 400,
                fast_slack_secs: 20_000,
                slow_block_secs: 900,
                slow_slack_secs: 20_000,
                max_lead_secs: 7_200,
                max_lag_secs: 3_600,
                time_settle_blocks: 6,
            },
            d_observe_secs: 600,
            d_confirm_secs: 3 * 3_600,
            d_refund_secs: 3 * 3_600,
            claim_closes_at_timelock: false,
            clock_failure_probability: "1e-6".into(),
            value_bands: vec![
                ValueBand { max_notional: 10_000, confirmations: 1, finalized_tag: false, min_evidence: EvidenceMethod::RpcQuorum },
                ValueBand { max_notional: 100_000, confirmations: 3, finalized_tag: false, min_evidence: EvidenceMethod::LightClient },
                ValueBand { max_notional: 1_000_000, confirmations: 6, finalized_tag: false, min_evidence: EvidenceMethod::OwnNode },
            ],
            fees: Fees { worst_lock: 50_000, worst_claim: 50_000, worst_refund: 50_000, raise: FeeRaise::RbfOrCpfp },
            refund: RefundMethod::Prepared,
            lock_id_binding: true,
            template_enforces_len32: true,
            clock_source: "reference values for tests, not measured".into(),
        }
    }

    pub fn ethereum() -> ChainProfile {
        ChainProfile {
            chain: ChainId::parse(ETHEREUM_MAINNET).unwrap(),
            mainnet: true,
            family: Family::Evm,
            // 12-second slots; missed slots make blocks slower.
            clock: ClockBounds {
                fast_block_secs: 12,
                fast_slack_secs: 0,
                slow_block_secs: 24,
                slow_slack_secs: 120,
                max_lead_secs: 15,
                max_lag_secs: 15,
                time_settle_blocks: 1,
            },
            d_observe_secs: 60,
            d_confirm_secs: 30 * 60,
            d_refund_secs: 30 * 60,
            claim_closes_at_timelock: true,
            clock_failure_probability: "1e-6".into(),
            value_bands: vec![
                ValueBand { max_notional: 1_000, confirmations: 3, finalized_tag: false, min_evidence: EvidenceMethod::RpcQuorum },
                ValueBand { max_notional: 1_000_000, confirmations: 0, finalized_tag: true, min_evidence: EvidenceMethod::LightClient },
            ],
            fees: Fees {
                worst_lock: 10_000_000_000_000_000,
                worst_claim: 10_000_000_000_000_000,
                worst_refund: 10_000_000_000_000_000,
                raise: FeeRaise::SameNonceReplacement,
            },
            refund: RefundMethod::Permissionless,
            lock_id_binding: true,
            template_enforces_len32: true,
            clock_source: "reference values for tests, not measured".into(),
        }
    }

    pub fn solana() -> ChainProfile {
        ChainProfile {
            chain: ChainId::parse(SOLANA_MAINNET).unwrap(),
            mainnet: true,
            family: Family::Solana,
            clock: ClockBounds {
                fast_block_secs: 0,
                fast_slack_secs: 0,
                slow_block_secs: 2,
                slow_slack_secs: 60,
                max_lead_secs: 60,
                max_lag_secs: 60,
                time_settle_blocks: 1,
            },
            d_observe_secs: 30,
            d_confirm_secs: 5 * 60,
            d_refund_secs: 5 * 60,
            claim_closes_at_timelock: true,
            clock_failure_probability: "1e-6".into(),
            value_bands: vec![
                ValueBand { max_notional: 1_000, confirmations: 32, finalized_tag: false, min_evidence: EvidenceMethod::RpcQuorum },
                ValueBand { max_notional: 1_000_000, confirmations: 0, finalized_tag: true, min_evidence: EvidenceMethod::OwnNode },
            ],
            fees: Fees { worst_lock: 1_000_000, worst_claim: 1_000_000, worst_refund: 1_000_000, raise: FeeRaise::Resubmit },
            refund: RefundMethod::Permissionless,
            lock_id_binding: true,
            template_enforces_len32: true,
            clock_source: "reference values for tests, not measured".into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reference_profiles_are_complete() {
        for p in [reference::bitcoin(reference::BITCOIN_MAINNET), reference::ethereum(), reference::solana()] {
            assert!(p.missing_obligatory().is_empty(), "{}: {:?}", p.chain, p.missing_obligatory());
        }
    }

    #[test]
    fn missing_items_are_reported() {
        let mut p = reference::ethereum();
        p.fees.raise = FeeRaise::None;
        p.refund = RefundMethod::None;
        p.template_enforces_len32 = false;
        assert_eq!(p.missing_obligatory().len(), 3);
    }

    #[test]
    fn clock_and_refund_rules() {
        let btc = reference::bitcoin(reference::BITCOIN_MAINNET);
        // No positive floor on Bitcoin block intervals is needed (spec 7.3).
        let floorless = ChainProfile { clock: ClockBounds { fast_block_secs: 0, fast_slack_secs: 0, ..btc.clock }, ..btc.clone() };
        assert!(floorless.missing_obligatory().is_empty());
        let check = |p: ChainProfile, item: &str| assert!(p.missing_obligatory().contains(&item), "{item}: {:?}", p.missing_obligatory());
        check(ChainProfile { clock: ClockBounds { fast_block_secs: 901, ..btc.clock }, ..btc.clone() }, "clock bounds");
        check(ChainProfile { clock: ClockBounds { time_settle_blocks: 5, ..btc.clock }, ..btc.clone() }, "clock bounds");
        check(ChainProfile { clock_failure_probability: " ".into(), ..btc.clone() }, "clock bound source");
        // A Bitcoin claim never closes at T, and an open claim needs a refund time.
        check(ChainProfile { claim_closes_at_timelock: true, ..btc.clone() }, "refund finality time");
        check(ChainProfile { d_refund_secs: 0, ..btc.clone() }, "refund finality time");
        assert_eq!(btc.d_refund(), 3 * 3_600);
        let eth = reference::ethereum();
        assert_eq!(eth.d_refund(), 0, "the reference EVM claim closes at T");
        assert!(ChainProfile { d_refund_secs: 0, ..eth }.missing_obligatory().is_empty());
    }

    #[test]
    fn bands_and_hash() {
        let p = reference::bitcoin(reference::BITCOIN_MAINNET);
        assert_eq!(p.band(5_000).unwrap().confirmations, 1);
        assert_eq!(p.band(50_000).unwrap().confirmations, 3);
        assert!(p.band(2_000_000).is_none());
        let mut q = p.clone();
        q.d_observe_secs += 1;
        assert_ne!(p.hash(), q.hash());
    }
}
