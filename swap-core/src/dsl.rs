//! The JSON mini-DSL of spec 6.3 and `validate_policy` (implementation spec 4.2).
//! The owner approves the exact policy text; its JCS hash is the `policy_hash`.

use crate::caip::{AssetId, ChainId, Family};
use crate::evm::ContractPin;
use crate::facts::{EvidenceMethod, OracleLimits};
use crate::profile::ProfileSet;
use crate::solana::ProgramPin;
use crate::types::RiskFlag;
use crate::verified::{Pair, SwapRule};
use crate::Hash32;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fmt;

/// How the policy pins a lock contract on one chain (S7, spec 8.4).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum ContractPinSpec {
    BitcoinTemplate(String),
    Evm(ContractPin),
    Solana(ProgramPin),
}

impl ContractPinSpec {
    fn family(&self) -> Family {
        match self {
            ContractPinSpec::BitcoinTemplate(_) => Family::Bitcoin,
            ContractPinSpec::Evm(_) => Family::Evm,
            ContractPinSpec::Solana(_) => Family::Solana,
        }
    }

    /// The `Lock.contract` value that this pin covers.
    pub fn contract_id(&self) -> String {
        match self {
            ContractPinSpec::BitcoinTemplate(t) => t.clone(),
            ContractPinSpec::Evm(p) => format!("0x{}", crate::to_hex(&p.address_bytes().unwrap_or_default())),
            ContractPinSpec::Solana(p) => p.program.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChainEntry {
    #[serde(with = "crate::enc::hex32")]
    pub profile_hash: Hash32,
    pub contracts: Vec<ContractPinSpec>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Authorities {
    #[serde(default)]
    pub oracle: Vec<String>,
    #[serde(default)]
    pub identity: Vec<String>,
    #[serde(default)]
    pub list: Vec<String>,
    #[serde(default)]
    pub collateral: Vec<String>,
}

/// The policy document as the owner approves it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SwapPolicy {
    pub version: u64,
    pub ref_ccy: String,
    #[serde(with = "crate::enc::hex32")]
    pub evaluator_id: Hash32,
    pub chains: BTreeMap<ChainId, ChainEntry>,
    #[serde(default)]
    pub authorities: Authorities,
    pub oracle: OracleLimits,
    /// Minimum providers for an RPC quorum (at least 2).
    pub quorum: usize,
    /// `D_margin` of S11 and S12.
    pub margin_secs: u64,
    pub rule: Value,
}

/// A validated policy, ready for the pipeline.
#[derive(Clone, Debug, PartialEq)]
pub struct CompiledPolicy {
    pub policy: SwapPolicy,
    pub rule: SwapRule,
    pub policy_hash: Hash32,
    /// Every period that a period atom names; the ledger tracks each one.
    pub periods: Vec<u64>,
}

impl CompiledPolicy {
    pub fn pins(&self, chain: &ChainId) -> &[ContractPinSpec] {
        self.policy.chains.get(chain).map(|c| &c.contracts[..]).unwrap_or(&[])
    }

    pub fn pin_for(&self, chain: &ChainId, contract: &str) -> Option<&ContractPinSpec> {
        self.pins(chain).iter().find(|p| p.contract_id().eq_ignore_ascii_case(contract))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PolicyError(pub String);

impl fmt::Display for PolicyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid policy: {}", self.0)
    }
}

impl std::error::Error for PolicyError {}

fn err<T>(msg: impl Into<String>) -> Result<T, PolicyError> {
    Err(PolicyError(msg.into()))
}

/// ISO 8601 durations `PT<n>H`, `P<n>D` and `P<n>W`, in seconds.
pub fn parse_period(text: &str) -> Option<u64> {
    let (digits, unit) = if let Some(rest) = text.strip_prefix("PT") {
        (rest.strip_suffix('H')?, 3_600)
    } else {
        let rest = text.strip_prefix('P')?;
        match rest.as_bytes().last()? {
            b'D' => (&rest[..rest.len() - 1], 86_400),
            b'W' => (&rest[..rest.len() - 1], 604_800),
            _ => return None,
        }
    };
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) || digits.starts_with('0') {
        return None;
    }
    digits.parse::<u64>().ok()?.checked_mul(unit)
}

struct Compiler<'a> {
    policy: &'a SwapPolicy,
    periods: Vec<u64>,
    chains_named: Vec<ChainId>,
}

impl Compiler<'_> {
    fn uint(v: &Value, what: &str) -> Result<u64, PolicyError> {
        v.as_u64().map_or_else(|| err(format!("{what}: expected a non-negative integer")), Ok)
    }

    fn strings<'v>(v: &'v Value, what: &str) -> Result<Vec<&'v str>, PolicyError> {
        let items = v.as_array().ok_or_else(|| PolicyError(format!("{what}: expected an array")))?;
        if items.is_empty() {
            return err(format!("{what}: empty set"));
        }
        items.iter().map(|i| i.as_str().ok_or_else(|| PolicyError(format!("{what}: expected strings")))).collect()
    }

    fn chain(&mut self, text: &str) -> Result<ChainId, PolicyError> {
        let chain = ChainId::parse(text).map_err(|e| PolicyError(e.to_string()))?;
        self.chains_named.push(chain.clone());
        Ok(chain)
    }

    fn asset(&mut self, text: &str) -> Result<AssetId, PolicyError> {
        let asset = AssetId::parse(text).map_err(|e| PolicyError(e.to_string()))?;
        self.chains_named.push(asset.chain());
        Ok(asset)
    }

    fn amount_in_ref(&self, v: &Value, what: &str) -> Result<u64, PolicyError> {
        match v.as_array().map(|a| &a[..]) {
            Some([ccy, amount]) => {
                if ccy.as_str() != Some(self.policy.ref_ccy.as_str()) {
                    return err(format!("{what}: currency differs from ref_ccy"));
                }
                Self::uint(amount, what)
            }
            _ => err(format!("{what}: expected [currency, amount]")),
        }
    }

    fn rule(&mut self, v: &Value) -> Result<SwapRule, PolicyError> {
        if let Some(name) = v.as_str() {
            return match name {
                "contract_pinned" => Ok(SwapRule::ContractPinned),
                _ => err(format!("unknown atom {name}")),
            };
        }
        let obj = v.as_object().ok_or_else(|| PolicyError("a rule is an object or an atom name".into()))?;
        if obj.len() != 1 {
            return err("a rule object has exactly one key");
        }
        let (key, arg) = obj.iter().next().unwrap();
        Ok(match key.as_str() {
            "all" | "any" => {
                let items = arg.as_array().ok_or_else(|| PolicyError(format!("{key}: expected an array")))?;
                if items.is_empty() {
                    return err(format!("empty {key}"));
                }
                let children = items.iter().map(|c| self.rule(c)).collect::<Result<Vec<_>, _>>()?;
                if key == "all" { SwapRule::All(children) } else { SwapRule::Any(children) }
            }
            "chain_in" => {
                let ids = Self::strings(arg, key)?.into_iter().map(|c| Ok(self.chain(c)?.id())).collect::<Result<_, PolicyError>>()?;
                SwapRule::ChainIn(ids)
            }
            "asset_in" => {
                let ids = Self::strings(arg, key)?.into_iter().map(|a| Ok(self.asset(a)?.id())).collect::<Result<_, PolicyError>>()?;
                SwapRule::AssetIn(ids)
            }
            "counterparty_in" => SwapRule::CounterpartyIn(
                Self::strings(arg, key)?.into_iter().map(|i| crate::sha256(i.as_bytes())).collect(),
            ),
            "counterparty_not_listed" => {
                let hash = arg.as_str().and_then(crate::from_hex_array).ok_or_else(|| PolicyError(format!("{key}: expected a 32-byte hex hash")))?;
                SwapRule::CounterpartyNotListed(hash)
            }
            "notional_at_most" => SwapRule::NotionalAtMost(self.amount_in_ref(arg, key)?),
            "period_notional_at_most" => match arg.as_array().map(|a| &a[..]) {
                Some([period, amount]) => {
                    let secs = period.as_str().and_then(parse_period).ok_or_else(|| PolicyError(format!("{key}: bad period")))?;
                    self.periods.push(secs);
                    SwapRule::PeriodNotionalAtMost(secs, Self::uint(amount, key)?)
                }
                _ => return err(format!("{key}: expected [period, amount]")),
            },
            "open_swaps_at_most" => SwapRule::OpenSwapsAtMost(Self::uint(arg, key)?),
            "evidence_at_least" => {
                let m = arg.as_str().and_then(EvidenceMethod::from_name).ok_or_else(|| PolicyError(format!("{key}: unknown method")))?;
                SwapRule::EvidenceAtLeast(m.level())
            }
            "pair_in" => {
                let pairs = arg.as_array().filter(|a| !a.is_empty()).ok_or_else(|| PolicyError(format!("{key}: expected pairs")))?;
                let mut out = Vec::new();
                for p in pairs {
                    match p.as_array().map(|a| &a[..]) {
                        Some([Value::String(g), Value::String(t)]) => {
                            out.push(Pair { give: self.asset(g)?.id(), take: self.asset(t)?.id() });
                        }
                        _ => return err(format!("{key}: expected [give, take]")),
                    }
                }
                SwapRule::PairIn(out)
            }
            "price_deviation_at_most" => SwapRule::PriceDeviationAtMost(Self::uint(arg, key)?),
            "timeout_gap_at_least" => SwapRule::TimeoutGapAtLeast(Self::uint(arg, key)?),
            "reveal_window_at_least" => SwapRule::RevealWindowAtLeast(Self::uint(arg, key)?),
            "finality_at_least" => match arg.as_array().map(|a| &a[..]) {
                Some([Value::String(chain), depth]) => SwapRule::FinalityAtLeast(self.chain(chain)?.id(), Self::uint(depth, key)?),
                _ => return err(format!("{key}: expected [chain, depth]")),
            },
            "asset_risk_within" => {
                let names = arg.as_array().ok_or_else(|| PolicyError(format!("{key}: expected an array")))?;
                let mut flags = Vec::new();
                for n in names {
                    let flag = n.as_str().and_then(RiskFlag::from_name).ok_or_else(|| PolicyError(format!("{key}: unknown flag")))?;
                    if flag.always_denied() {
                        return err(format!("{key}: {} always denies and cannot be allowed", flag.name()));
                    }
                    flags.push(flag);
                }
                SwapRule::AssetRiskWithin(RiskFlag::mask(&flags))
            }
            "contract_pinned" => {
                if arg != &Value::Bool(true) {
                    return err("contract_pinned takes true");
                }
                SwapRule::ContractPinned
            }
            "collateral_at_least" => SwapRule::CollateralAtLeast(Self::uint(arg, key)?),
            other => return err(format!("unknown atom {other}")),
        })
    }
}

/// Parse and validate a policy against the profiles that the signer runs.
pub fn validate_policy(text: &str, profiles: &ProfileSet) -> Result<CompiledPolicy, PolicyError> {
    let value: Value = serde_json::from_str(text).map_err(|e| PolicyError(e.to_string()))?;
    let policy: SwapPolicy = serde_json::from_value(value.clone()).map_err(|e| PolicyError(e.to_string()))?;
    if policy.chains.is_empty() {
        return err("no chains");
    }
    if policy.quorum < 2 {
        return err("quorum below 2");
    }
    if policy.ref_ccy.len() != 3 || !policy.ref_ccy.bytes().all(|c| c.is_ascii_uppercase()) {
        return err("ref_ccy is an ISO 4217 code");
    }
    for (chain, entry) in &policy.chains {
        let profile = profiles.get(chain).ok_or_else(|| PolicyError(format!("{chain}: no profile")))?;
        let missing = profile.missing_obligatory();
        if !missing.is_empty() {
            return err(format!("{chain}: profile misses {}", missing.join(", ")));
        }
        if profile.hash() != entry.profile_hash {
            return err(format!("{chain}: profile hash differs from the pinned hash"));
        }
        if entry.contracts.is_empty() {
            return err(format!("{chain}: no pinned contract"));
        }
        if entry.contracts.iter().any(|c| Some(c.family()) != chain.family()) {
            return err(format!("{chain}: contract pin of another chain family"));
        }
        for c in &entry.contracts {
            let ok = match c {
                ContractPinSpec::BitcoinTemplate(t) => t == crate::bitcoin::TEMPLATE_ID,
                ContractPinSpec::Evm(p) => p.well_formed(),
                ContractPinSpec::Solana(p) => p.well_formed(),
            };
            if !ok {
                return err(format!("{chain}: malformed contract pin"));
            }
        }
        // S7 uses the first pin of a contract (`pin_for`): a second pin of the same
        // contract, for example with another code hash, would never apply.
        for (i, c) in entry.contracts.iter().enumerate() {
            if entry.contracts[..i].iter().any(|d| d.contract_id().eq_ignore_ascii_case(&c.contract_id())) {
                return err(format!("{chain}: contract pinned twice"));
            }
        }
    }
    let mut compiler = Compiler { policy: &policy, periods: Vec::new(), chains_named: Vec::new() };
    let rule = compiler.rule(&policy.rule)?;
    for chain in &compiler.chains_named {
        if !policy.chains.contains_key(chain) {
            return err(format!("{chain}: named in the rule without a chain entry"));
        }
    }
    let mut periods = compiler.periods;
    periods.sort_unstable();
    periods.dedup();
    let policy_hash = crate::blake3(crate::jcs::canonicalize(&value).map_err(|e| PolicyError(e.to_string()))?.as_bytes());
    Ok(CompiledPolicy { policy, rule, policy_hash, periods })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::profile::reference;

    pub(crate) const BTC: &str = "bip122:000000000019d6689c085ae165831e93/slip44:0";
    pub(crate) const USDC: &str = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48";
    pub(crate) const HTLC_EVM: &str = "0x3333333333333333333333333333333333333333";

    pub(crate) fn profiles() -> ProfileSet {
        ProfileSet::new(vec![reference::bitcoin(reference::BITCOIN_MAINNET), reference::ethereum(), reference::solana()])
    }

    pub(crate) fn policy_json(rule: Value) -> Value {
        let p = profiles();
        let btc = p.get(&ChainId::parse(reference::BITCOIN_MAINNET).unwrap()).unwrap();
        let eth = p.get(&ChainId::parse(reference::ETHEREUM_MAINNET).unwrap()).unwrap();
        serde_json::json!({
            "version": 3,
            "ref_ccy": "USD",
            "evaluator_id": crate::to_hex(&[0xee; 32]),
            "chains": {
                (reference::BITCOIN_MAINNET): { "profile_hash": crate::to_hex(&btc.hash()), "contracts": [{"bitcoin_template": crate::bitcoin::TEMPLATE_ID}] },
                (reference::ETHEREUM_MAINNET): { "profile_hash": crate::to_hex(&eth.hash()), "contracts": [{"evm": {"address": HTLC_EVM, "code_hash": crate::to_hex(&[0xc0; 32])}}] }
            },
            "authorities": { "oracle": ["pyth"], "identity": ["mnemonik"], "list": ["ofac-mirror"] },
            "oracle": { "max_age_secs": 60, "max_conf_bps": 50 },
            "quorum": 2,
            "margin_secs": 1800,
            "rule": rule
        })
    }

    /// The example policy of spec 6.3.
    pub(crate) fn example_rule() -> Value {
        serde_json::json!({ "all": [
            { "pair_in": [[BTC, USDC]] },
            { "notional_at_most": ["USD", 50000] },
            { "period_notional_at_most": ["P1D", 200000] },
            { "price_deviation_at_most": 50 },
            { "timeout_gap_at_least": 7200 },
            { "finality_at_least": [reference::BITCOIN_MAINNET, 3] },
            { "evidence_at_least": "light_client" },
            { "asset_risk_within": ["freezable_by_issuer"] },
            { "any": [ { "counterparty_in": ["did:key:z6MkResponder"] },
                       { "notional_at_most": ["USD", 1000] } ] }
        ]})
    }

    #[test]
    fn example_policy_compiles() {
        let c = validate_policy(&policy_json(example_rule()).to_string(), &profiles()).unwrap();
        assert_eq!(c.periods, vec![86_400]);
        let SwapRule::All(children) = &c.rule else { panic!() };
        assert_eq!(children.len(), 9);
        assert_eq!(children[1], SwapRule::NotionalAtMost(50_000));
        assert_eq!(children[7], SwapRule::AssetRiskWithin(RiskFlag::FreezableByIssuer.bit()));
        // Whitespace and key order do not change the policy hash.
        let pretty = serde_json::to_string_pretty(&policy_json(example_rule())).unwrap();
        assert_eq!(validate_policy(&pretty, &profiles()).unwrap().policy_hash, c.policy_hash);
    }

    #[test]
    fn rejections() {
        let p = profiles();
        let bad_rules = [
            ("empty all", serde_json::json!({"all": []})),
            ("empty any", serde_json::json!({"any": []})),
            ("unknown atom", serde_json::json!({"always": true})),
            ("other currency", serde_json::json!({"notional_at_most": ["EUR", 5]})),
            ("forbidden flag", serde_json::json!({"asset_risk_within": ["confidential_amount"]})),
            ("non_transferable", serde_json::json!({"asset_risk_within": ["non_transferable"]})),
            ("chain without entry", serde_json::json!({"chain_in": ["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"]})),
            ("asset without entry", serde_json::json!({"asset_in": ["eip155:10/slip44:60"]})),
            ("bad period", serde_json::json!({"period_notional_at_most": ["P0D", 5]})),
            ("two keys", serde_json::json!({"open_swaps_at_most": 1, "collateral_at_least": 2})),
            ("negative", serde_json::json!({"open_swaps_at_most": -1})),
            ("exit action", serde_json::json!({"refund": "deny"})),
        ];
        for (name, rule) in bad_rules {
            assert!(validate_policy(&policy_json(rule).to_string(), &p).is_err(), "{name}");
        }
        let mut doc = policy_json(example_rule());
        doc["claim"] = serde_json::json!({"deny": true});
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "a policy field that names an exit action");
        let mut doc = policy_json(example_rule());
        doc["chains"][reference::ETHEREUM_MAINNET]["profile_hash"] = Value::String(crate::to_hex(&[0; 32]));
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "profile hash pin");
        let mut doc = policy_json(example_rule());
        doc["quorum"] = Value::from(1);
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "quorum");
        let mut doc = policy_json(example_rule());
        doc["chains"][reference::BITCOIN_MAINNET]["contracts"] = serde_json::json!([{"bitcoin_template": "other"}]);
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "unknown template");
        // Version 1 of the template has no lock_id in the claim leaf (G4).
        doc["chains"][reference::BITCOIN_MAINNET]["contracts"] = serde_json::json!([{"bitcoin_template": "warrant-htlc-tr-v1"}]);
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "template version 1");
        // A proxy pin needs parseable admin and implementation addresses and the implementation code hash.
        let proxy = |admin: &str| serde_json::json!([{"evm": {"address": HTLC_EVM, "code_hash": crate::to_hex(&[0xc0; 32]), "proxy": {"admin": admin, "implementation": crate::to_hex(&[7; 20]), "implementation_code_hash": crate::to_hex(&[9; 32])}}}]);
        let mut doc = policy_json(example_rule());
        doc["chains"][reference::ETHEREUM_MAINNET]["contracts"] = proxy(&crate::to_hex(&[8; 20]));
        assert!(validate_policy(&doc.to_string(), &p).is_ok(), "a well-formed proxy pin");
        doc["chains"][reference::ETHEREUM_MAINNET]["contracts"] = proxy("0x08");
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "malformed proxy admin");
        doc["chains"][reference::ETHEREUM_MAINNET]["contracts"][0]["evm"]["proxy"]["admin"] = Value::String(crate::to_hex(&[8; 20]));
        doc["chains"][reference::ETHEREUM_MAINNET]["contracts"][0]["evm"]["proxy"].as_object_mut().unwrap().remove("implementation_code_hash");
        assert!(validate_policy(&doc.to_string(), &p).is_err(), "proxy pin without the implementation code hash");
        // Spec 8.4 Solana (G21): a program pin carries the code hash and a well-formed
        // upgrade authority.
        let ps = ProfileSet::new(vec![reference::bitcoin(reference::BITCOIN_MAINNET), reference::ethereum(), reference::solana()]);
        let program = bs58::encode([2u8; 32]).into_string();
        let solana = |pin: Value| {
            let mut doc = policy_json(example_rule());
            doc["chains"][reference::SOLANA_MAINNET] = serde_json::json!({
                "profile_hash": crate::to_hex(&reference::solana().hash()), "contracts": [{"solana": pin}]
            });
            validate_policy(&doc.to_string(), &ps)
        };
        let code_hash = crate::to_hex(&[0xc5; 32]);
        assert!(solana(serde_json::json!({"program": program, "code_hash": code_hash})).is_ok());
        assert!(solana(serde_json::json!({"program": program, "code_hash": code_hash, "upgrade_authority": bs58::encode([3u8; 32]).into_string()})).is_ok());
        assert!(solana(serde_json::json!({"program": program})).is_err(), "no code hash");
        assert!(solana(serde_json::json!({"program": program, "code_hash": "c5"})).is_err(), "short code hash");
        assert!(solana(serde_json::json!({"program": program, "code_hash": code_hash, "upgrade_authority": "0OIl"})).is_err(), "malformed authority");
        assert!(solana(serde_json::json!({"program": program, "code_hash": code_hash, "loader": "BPFLoader2111111111111111111111111111111111"})).is_err(), "unknown field");
        // One pin per contract: a second pin of the same program or address is rejected.
        let mut doc = policy_json(example_rule());
        doc["chains"][reference::SOLANA_MAINNET] = serde_json::json!({
            "profile_hash": crate::to_hex(&reference::solana().hash()),
            "contracts": [{"solana": {"program": program, "code_hash": code_hash}}, {"solana": {"program": program, "code_hash": crate::to_hex(&[0xc6; 32])}}]
        });
        let twice = |doc: &Value| format!("{:?}", validate_policy(&doc.to_string(), &ps).unwrap_err()).contains("pinned twice");
        assert!(twice(&doc), "program pinned twice");
        // `pin_for` compares ids without case, so two program ids that differ only in
        // case are one pin too.
        let alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
        let other_case = (0..program.len())
            .find_map(|i| {
                let c = program.as_bytes()[i] as char;
                let swapped = if c.is_ascii_lowercase() { c.to_ascii_uppercase() } else { c.to_ascii_lowercase() };
                let candidate = format!("{}{swapped}{}", &program[..i], &program[i + 1..]);
                (swapped != c && alphabet.contains(swapped) && crate::solana::parse_key(&candidate).is_some()).then_some(candidate)
            })
            .unwrap();
        doc["chains"][reference::SOLANA_MAINNET]["contracts"][1]["solana"]["program"] = Value::String(other_case);
        assert!(twice(&doc), "program pinned twice, in another case");
        let mut doc = policy_json(example_rule());
        let evm = doc["chains"][reference::ETHEREUM_MAINNET]["contracts"][0].clone();
        let mut upper = evm.clone();
        upper["evm"]["address"] = Value::String(upper["evm"]["address"].as_str().unwrap().to_uppercase().replacen("0X", "0x", 1));
        doc["chains"][reference::ETHEREUM_MAINNET]["contracts"] = serde_json::json!([evm, upper]);
        assert!(twice(&doc), "address pinned twice, in another case");
        // A profile that misses an obligatory item makes every policy naming the chain invalid.
        let mut broken = reference::ethereum();
        broken.refund = crate::profile::RefundMethod::None;
        let p2 = ProfileSet::new(vec![reference::bitcoin(reference::BITCOIN_MAINNET), broken]);
        assert!(validate_policy(&policy_json(example_rule()).to_string(), &p2).is_err());
    }

    #[test]
    fn periods() {
        assert_eq!(parse_period("PT1H"), Some(3_600));
        assert_eq!(parse_period("P1D"), Some(86_400));
        assert_eq!(parse_period("P2W"), Some(1_209_600));
        for bad in ["P", "PD", "P1", "P1M", "PT1D", "P01D", "P-1D", "1D"] {
            assert_eq!(parse_period(bad), None, "{bad}");
        }
    }
}
