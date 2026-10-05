//! Facts with provenance (spec section 5). A bare assertion from the agent is
//! never a fact: every value here comes from a chain observation, a signed
//! report, the ledger or a deterministic derivation. Anything else is unknown.

use crate::caip::AssetId;
use crate::Hash32;
use serde::{Deserialize, Serialize};

/// Observation methods, weakest first (spec 5.3). The order is the evaluator's
/// `EvidenceAtLeast` scale.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceMethod {
    SingleRpc = 0,
    RpcQuorum = 1,
    LightClient = 2,
    OwnNode = 3,
}

impl EvidenceMethod {
    pub fn level(self) -> u8 {
        self as u8
    }

    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "single_rpc" => Some(EvidenceMethod::SingleRpc),
            "rpc_quorum" => Some(EvidenceMethod::RpcQuorum),
            "light_client" => Some(EvidenceMethod::LightClient),
            "own_node" => Some(EvidenceMethod::OwnNode),
            _ => None,
        }
    }
}

/// Where a fact came from. Recorded in the warrant.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "class", rename_all = "snake_case")]
pub enum Provenance {
    Signed { authority: String },
    Chain {
        method: EvidenceMethod,
        #[serde(with = "crate::enc::hex32")]
        block_hash: Hash32,
        height: u64,
        providers: Vec<String>,
    },
    Derived { inputs: Vec<String> },
    Ledger { counter: u64 },
}

/// One provider's report of a chain value at a block.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Report<T> {
    pub provider: String,
    pub block_hash: Hash32,
    pub height: u64,
    pub value: T,
}

/// A chain value as observed through one method by one or more providers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observed<T> {
    pub method: EvidenceMethod,
    pub reports: Vec<Report<T>>,
}

impl<T: Clone + PartialEq> Observed<T> {
    pub fn single(method: EvidenceMethod, provider: &str, block_hash: Hash32, height: u64, value: T) -> Self {
        Observed {
            method,
            reports: vec![Report { provider: provider.into(), block_hash, height, value }],
        }
    }

    /// The value, if the observation is valid: enough distinct providers for the
    /// method, and every provider reports the same block and the same value.
    /// Disagreement gives unknown (spec 5.3).
    pub fn resolve(&self, quorum: usize) -> Option<(T, Provenance)> {
        let first = self.reports.first()?;
        let mut providers: Vec<String> = self.reports.iter().map(|r| r.provider.clone()).collect();
        providers.sort();
        providers.dedup();
        if providers.len() != self.reports.len() {
            return None;
        }
        let needed = match self.method {
            EvidenceMethod::RpcQuorum => quorum.max(2),
            _ => 1,
        };
        if self.reports.len() < needed {
            return None;
        }
        if self.method != EvidenceMethod::RpcQuorum && self.reports.len() != 1 {
            return None;
        }
        let agree = self.reports.iter().all(|r| {
            r.block_hash == first.block_hash && r.height == first.height && r.value == first.value
        });
        if !agree {
            return None;
        }
        Some((
            first.value.clone(),
            Provenance::Chain { method: self.method, block_hash: first.block_hash, height: first.height, providers },
        ))
    }
}

/// A signed price report (spec 5.4): price of one whole unit of `asset` in the
/// reference currency, scaled by 10^8. Signature verification happens before
/// this struct exists; `signature_ok` records its result.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceReport {
    pub asset: AssetId,
    pub currency: String,
    pub price_e8: u128,
    pub conf_e8: u128,
    pub publish_time: u64,
    pub authority: String,
    pub signature_ok: bool,
}

/// Oracle acceptance limits from the policy.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OracleLimits {
    pub max_age_secs: u64,
    pub max_conf_bps: u64,
}

impl PriceReport {
    /// The price, if the report is from an approved authority, is signed, is fresh
    /// and has a narrow enough confidence interval. Otherwise unknown.
    pub fn accept(&self, currency: &str, authorities: &[String], limits: &OracleLimits, now: u64) -> Option<u128> {
        if !self.signature_ok || self.currency != currency || !authorities.contains(&self.authority) {
            return None;
        }
        if self.publish_time > now || now - self.publish_time > limits.max_age_secs || self.price_e8 == 0 {
            return None;
        }
        // conf / price ≤ max_conf_bps / 10_000
        if self.conf_e8.checked_mul(10_000)? > self.price_e8.checked_mul(limits.max_conf_bps as u128)? {
            return None;
        }
        Some(self.price_e8)
    }
}

/// Value of `amount` base units in whole units of the reference currency, rounded up.
pub fn value_in_ref(amount: u128, decimals: u8, price_e8: u128) -> Option<u64> {
    let numerator = amount.checked_mul(price_e8)?;
    let denominator = 10u128.checked_pow(decimals as u32)?.checked_mul(100_000_000)?;
    let whole = numerator.div_ceil(denominator);
    u64::try_from(whole).ok()
}

/// Price deviation of the trade in basis points: how far the value taken is from
/// the value given, relative to the value given, rounded up.
pub fn deviation_bps(give_value_e8: u128, take_value_e8: u128) -> Option<u64> {
    if give_value_e8 == 0 {
        return None;
    }
    let diff = give_value_e8.abs_diff(take_value_e8);
    u64::try_from(diff.checked_mul(10_000)?.div_ceil(give_value_e8)).ok()
}

/// Value in the reference currency scaled by 10^8 (no rounding loss for deviation).
pub fn value_e8(amount: u128, decimals: u8, price_e8: u128) -> Option<u128> {
    amount.checked_mul(price_e8)?.checked_div(10u128.checked_pow(decimals as u32)?)
}

/// A transfer fee read from the chain: `fee = round(gross · bps / 10_000)`, capped
/// at `max_fee` (Token-2022 rounds up; many EVM fee tokens round down).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransferFee {
    pub bps: u16,
    pub max_fee: Option<u128>,
    pub round_up: bool,
}

impl TransferFee {
    pub fn fee(&self, gross: u128) -> Option<u128> {
        let product = gross.checked_mul(self.bps as u128)?;
        let fee = if self.round_up { product.div_ceil(10_000) } else { product / 10_000 };
        Some(self.max_fee.map_or(fee, |m| fee.min(m)))
    }

    pub fn net(&self, gross: u128) -> Option<u128> {
        gross.checked_sub(self.fee(gross)?)
    }

    /// The smallest gross debit whose net amount is exactly `net`; `None` when no
    /// gross debit gives exactly `net` (then S8 cannot hold and the lock is denied).
    pub fn gross_for_net(&self, net: u128) -> Option<u128> {
        if self.bps >= 10_000 {
            return None;
        }
        // net(g) is non-decreasing in g; binary search the first g with net(g) >= net.
        // Upper bound: net(g) ≥ g·(1 − bps/10_000) − 1, and with a cap net(g) ≥ g − max_fee.
        let uncapped = net.checked_add(2)?.checked_mul(10_000)? / (10_000 - self.bps as u128) + 2;
        let mut hi = match self.max_fee {
            Some(m) => uncapped.min(net.checked_add(m)?.checked_add(1)?),
            None => uncapped,
        };
        let mut lo = net;
        if self.net(hi)? < net {
            return None;
        }
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.net(mid)? >= net {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        (self.net(lo)? == net).then_some(lo)
    }
}

/// A fact as recorded in the warrant: name, JSON value and provenance.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FactRecord {
    pub name: String,
    /// `null` when unknown.
    pub value: serde_json::Value,
    pub provenance: Option<Provenance>,
}

impl FactRecord {
    pub fn new(name: &str, value: serde_json::Value, provenance: Option<Provenance>) -> Self {
        FactRecord { name: name.into(), value, provenance }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(p: &str, block: u8, value: u64) -> Report<u64> {
        Report { provider: p.into(), block_hash: [block; 32], height: 10, value }
    }

    #[test]
    fn quorum_agreement() {
        let ok = Observed { method: EvidenceMethod::RpcQuorum, reports: vec![report("a", 1, 5), report("b", 1, 5)] };
        let (v, prov) = ok.resolve(2).unwrap();
        assert_eq!(v, 5);
        assert!(matches!(prov, Provenance::Chain { method: EvidenceMethod::RpcQuorum, .. }));
        // Fault test 5: providers disagree on the value or on the block.
        let value = Observed { method: EvidenceMethod::RpcQuorum, reports: vec![report("a", 1, 5), report("b", 1, 6)] };
        assert!(value.resolve(2).is_none());
        let block = Observed { method: EvidenceMethod::RpcQuorum, reports: vec![report("a", 1, 5), report("b", 2, 5)] };
        assert!(block.resolve(2).is_none());
        let short = Observed { method: EvidenceMethod::RpcQuorum, reports: vec![report("a", 1, 5), report("b", 1, 5)] };
        assert!(short.resolve(3).is_none());
        let dup = Observed { method: EvidenceMethod::RpcQuorum, reports: vec![report("a", 1, 5), report("a", 1, 5)] };
        assert!(dup.resolve(2).is_none());
        let empty: Observed<u64> = Observed { method: EvidenceMethod::OwnNode, reports: vec![] };
        assert!(empty.resolve(1).is_none());
    }

    #[test]
    fn oracle_limits() {
        let limits = OracleLimits { max_age_secs: 60, max_conf_bps: 20 };
        let auth = vec!["pyth".to_string()];
        let mut r = PriceReport {
            asset: AssetId::parse("bip122:000000000019d6689c085ae165831e93/slip44:0").unwrap(),
            currency: "USD".into(),
            price_e8: 60_000_00000000,
            conf_e8: 100_00000000,
            publish_time: 1_000,
            authority: "pyth".into(),
            signature_ok: true,
        };
        assert_eq!(r.accept("USD", &auth, &limits, 1_030), Some(60_000_00000000));
        assert_eq!(r.accept("USD", &auth, &limits, 1_061), None, "stale");
        assert_eq!(r.accept("EUR", &auth, &limits, 1_030), None, "currency");
        assert_eq!(r.accept("USD", &[], &limits, 1_030), None, "authority");
        r.conf_e8 = 121_00000000;
        assert_eq!(r.accept("USD", &auth, &limits, 1_030), None, "wide confidence");
        r.conf_e8 = 0;
        r.signature_ok = false;
        assert_eq!(r.accept("USD", &auth, &limits, 1_030), None, "unsigned");
    }

    #[test]
    fn gross_debit_for_fee_tokens() {
        let floor = TransferFee { bps: 100, max_fee: None, round_up: false };
        let g = floor.gross_for_net(1_000_000).unwrap();
        assert_eq!(floor.net(g), Some(1_000_000));
        assert!(floor.net(g - 1).unwrap() < 1_000_000, "smallest gross");
        let capped = TransferFee { bps: 500, max_fee: Some(10), round_up: true };
        assert_eq!(capped.gross_for_net(1_000_000), Some(1_000_010));
        let none = TransferFee { bps: 0, max_fee: None, round_up: false };
        assert_eq!(none.gross_for_net(42), Some(42));
        assert_eq!(TransferFee { bps: 10_000, max_fee: None, round_up: false }.gross_for_net(1), None);
        // A 99.99% fee: one unit of net needs about 10,000 units of gross.
        let steep = TransferFee { bps: 9_999, max_fee: None, round_up: true };
        assert_eq!(steep.net(10_000), Some(1));
        assert!(steep.gross_for_net(2).is_some());
        for net in [1u128, 7, 999, 123_456_789] {
            for fee in [floor, capped, steep] {
                if let Some(g) = fee.gross_for_net(net) {
                    assert_eq!(fee.net(g), Some(net));
                }
            }
        }
    }

    #[test]
    fn notional_and_deviation() {
        // 0.5 BTC at 60,000.00000001 USD rounds up to 30,001.
        assert_eq!(value_in_ref(50_000_000, 8, 60_000_00000001), Some(30_001));
        assert_eq!(value_in_ref(50_000_000, 8, 60_000_00000000), Some(30_000));
        assert_eq!(value_in_ref(u128::MAX, 0, 2), None);
        assert_eq!(deviation_bps(10_000, 9_950), Some(50));
        assert_eq!(deviation_bps(10_000, 10_051), Some(51));
        assert_eq!(deviation_bps(0, 1), None);
    }
}
