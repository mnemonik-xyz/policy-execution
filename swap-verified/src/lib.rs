//! Swap policy evaluation and timeout arithmetic. Executable code and its Verus
//! specification share this source. Fact building, the obligatory safety checks,
//! transaction decoding and signing are outside this proof (see `swap-core`).
use vstd::prelude::*;

verus! {

// ---------------------------------------------------------------------------
// Rules and facts
// ---------------------------------------------------------------------------

/// An ordered asset pair: the asset this party gives and the asset it takes.
#[cfg_attr(feature = "serde", derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
pub struct Pair {
    pub give: [u8; 32],
    pub take: [u8; 32],
}

/// Identifiers are 32-byte hashes (of CAIP strings, identities or list snapshots).
/// Every atom is monotone; the grammar has no negation.
#[cfg_attr(feature = "serde", derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "serde", serde(rename_all = "snake_case"))]
pub enum SwapRule {
    All(Vec<SwapRule>),
    Any(Vec<SwapRule>),
    /// Both leg chains are in the set.
    ChainIn(Vec<[u8; 32]>),
    /// Both leg assets are in the set.
    AssetIn(Vec<[u8; 32]>),
    CounterpartyIn(Vec<[u8; 32]>),
    /// A signed list snapshot with this hash does not contain the counterparty.
    CounterpartyNotListed([u8; 32]),
    /// Notional in whole units of the policy reference currency.
    NotionalAtMost(u64),
    /// (period in seconds, cap): ledger spend in the period plus this trade.
    PeriodNotionalAtMost(u64, u64),
    OpenSwapsAtMost(u64),
    /// 0 single RPC, 1 RPC quorum, 2 light client, 3 own full node.
    EvidenceAtLeast(u8),
    PairIn(Vec<Pair>),
    PriceDeviationAtMost(u64),
    TimeoutGapAtLeast(u64),
    RevealWindowAtLeast(u64),
    /// (chain, depth): the counterparty lock on this chain is final.
    FinalityAtLeast([u8; 32], u64),
    /// Bit set of allowed asset risk flags.
    AssetRiskWithin(u32),
    ContractPinned,
    CollateralAtLeast(u64),
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub struct ListCheck {
    pub list_hash: [u8; 32],
    pub listed: bool,
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub struct PeriodSpent {
    pub period: u64,
    pub spent: Option<u64>,
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub struct PeriodSpentKnown {
    pub period: u64,
    pub spent: u64,
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub struct LockObs {
    pub chain: [u8; 32],
    pub depth: Option<u64>,
    pub finalized: Option<bool>,
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub struct LockObsKnown {
    pub chain: [u8; 32],
    pub depth: u64,
    pub finalized: bool,
}

/// Complete facts: every value is known.
#[cfg_attr(feature = "serde", derive(Clone, Debug, PartialEq, Eq))]
pub struct SwapFacts {
    pub give_chain: [u8; 32],
    pub take_chain: [u8; 32],
    pub give_asset: [u8; 32],
    pub take_asset: [u8; 32],
    pub counterparty: [u8; 32],
    pub list_check: ListCheck,
    pub notional: u64,
    pub period_spent: Vec<PeriodSpentKnown>,
    pub open_swaps: u64,
    pub evidence: u8,
    pub price_deviation_bps: u64,
    pub timeout_gap: u64,
    pub reveal_window: u64,
    /// `None`: the action does not depend on a counterparty lock.
    pub counterparty_lock: Option<LockObsKnown>,
    pub asset_risk: u32,
    pub contract_pinned: bool,
    pub collateral_bps: u64,
}

/// Facts that may be unknown. The chains and assets select the trade and are
/// always known. `counterparty_lock: None` means "not applicable", not unknown.
#[cfg_attr(feature = "serde", derive(Clone, Debug, PartialEq, Eq))]
pub struct SwapFacts3 {
    pub give_chain: [u8; 32],
    pub take_chain: [u8; 32],
    pub give_asset: [u8; 32],
    pub take_asset: [u8; 32],
    pub counterparty: Option<[u8; 32]>,
    pub list_check: Option<ListCheck>,
    pub notional: Option<u64>,
    pub period_spent: Vec<PeriodSpent>,
    pub open_swaps: Option<u64>,
    pub evidence: Option<u8>,
    pub price_deviation_bps: Option<u64>,
    pub timeout_gap: Option<u64>,
    pub reveal_window: Option<u64>,
    pub counterparty_lock: Option<LockObs>,
    pub asset_risk: Option<u32>,
    pub contract_pinned: Option<bool>,
    pub collateral_bps: Option<u64>,
}

#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq))]
pub enum Decision {
    Allow,
    Deny,
    Ask,
}

// ---------------------------------------------------------------------------
// Declarative meaning on complete facts
// ---------------------------------------------------------------------------

pub open spec fn id_in(set: Seq<[u8; 32]>, id: [u8; 32]) -> bool {
    exists|i: int| 0 <= i < set.len() && (#[trigger] set[i])@ == id@
}

pub open spec fn pair_in(pairs: Seq<Pair>, give: [u8; 32], take: [u8; 32]) -> bool {
    exists|i: int| 0 <= i < pairs.len()
        && (#[trigger] pairs[i]).give@ == give@ && pairs[i].take@ == take@
}

pub open spec fn period_ok(entries: Seq<PeriodSpentKnown>, period: u64, notional: u64, cap: u64) -> bool {
    &&& exists|i: int| 0 <= i < entries.len() && (#[trigger] entries[i]).period == period
    &&& forall|i: int| 0 <= i < entries.len() && (#[trigger] entries[i]).period == period
        ==> entries[i].spent as int + notional as int <= cap as int
}

pub open spec fn finality_ok(lock: Option<LockObsKnown>, chain: [u8; 32], depth: u64) -> bool {
    match lock {
        None => true,
        Some(l) => l.chain@ != chain@ || l.finalized || l.depth >= depth,
    }
}

pub open spec fn satisfies(rule: &SwapRule, facts: &SwapFacts) -> bool
    decreases rule,
{
    match rule {
        SwapRule::All(children) => forall|i: int| 0 <= i < children.len()
            ==> satisfies(#[trigger] &children[i], facts),
        SwapRule::Any(children) => exists|i: int| 0 <= i < children.len()
            && satisfies(#[trigger] &children[i], facts),
        SwapRule::ChainIn(set) => id_in(set@, facts.give_chain) && id_in(set@, facts.take_chain),
        SwapRule::AssetIn(set) => id_in(set@, facts.give_asset) && id_in(set@, facts.take_asset),
        SwapRule::CounterpartyIn(set) => id_in(set@, facts.counterparty),
        SwapRule::CounterpartyNotListed(hash) => facts.list_check.list_hash@ == hash@
            && !facts.list_check.listed,
        SwapRule::NotionalAtMost(cap) => facts.notional <= *cap,
        SwapRule::PeriodNotionalAtMost(period, cap) =>
            period_ok(facts.period_spent@, *period, facts.notional, *cap),
        SwapRule::OpenSwapsAtMost(n) => facts.open_swaps <= *n,
        SwapRule::EvidenceAtLeast(m) => facts.evidence >= *m,
        SwapRule::PairIn(pairs) => pair_in(pairs@, facts.give_asset, facts.take_asset),
        SwapRule::PriceDeviationAtMost(bps) => facts.price_deviation_bps <= *bps,
        SwapRule::TimeoutGapAtLeast(secs) => facts.timeout_gap >= *secs,
        SwapRule::RevealWindowAtLeast(secs) => facts.reveal_window >= *secs,
        SwapRule::FinalityAtLeast(chain, depth) => finality_ok(facts.counterparty_lock, *chain, *depth),
        SwapRule::AssetRiskWithin(allowed) => facts.asset_risk & !*allowed == 0,
        SwapRule::ContractPinned => facts.contract_pinned,
        SwapRule::CollateralAtLeast(bps) => facts.collateral_bps >= *bps,
    }
}

// ---------------------------------------------------------------------------
// Partial facts and strong Kleene meaning
// ---------------------------------------------------------------------------

pub open spec fn agrees<T>(known: Option<T>, value: T) -> bool {
    match known {
        Some(v) => v == value,
        None => true,
    }
}

pub open spec fn lock_completes(l3: Option<LockObs>, l: Option<LockObsKnown>) -> bool {
    match (l3, l) {
        (None, None) => true,
        (Some(a), Some(b)) => a.chain@ == b.chain@ && agrees(a.depth, b.depth)
            && agrees(a.finalized, b.finalized),
        _ => false,
    }
}

pub open spec fn list_completes(c3: Option<ListCheck>, c: ListCheck) -> bool {
    match c3 {
        Some(a) => a.list_hash@ == c.list_hash@ && a.listed == c.listed,
        None => true,
    }
}

pub open spec fn id_completes(i3: Option<[u8; 32]>, i: [u8; 32]) -> bool {
    match i3 {
        Some(a) => a@ == i@,
        None => true,
    }
}

/// `facts` is one way of filling in every unknown in `f3`.
pub open spec fn completes(f3: &SwapFacts3, facts: &SwapFacts) -> bool {
    &&& facts.give_chain@ == f3.give_chain@
    &&& facts.take_chain@ == f3.take_chain@
    &&& facts.give_asset@ == f3.give_asset@
    &&& facts.take_asset@ == f3.take_asset@
    &&& id_completes(f3.counterparty, facts.counterparty)
    &&& list_completes(f3.list_check, facts.list_check)
    &&& agrees(f3.notional, facts.notional)
    &&& facts.period_spent.len() == f3.period_spent.len()
    &&& forall|i: int| 0 <= i < f3.period_spent.len()
        ==> (#[trigger] f3.period_spent[i]).period == facts.period_spent[i].period
            && agrees(f3.period_spent[i].spent, facts.period_spent[i].spent)
    &&& agrees(f3.open_swaps, facts.open_swaps)
    &&& agrees(f3.evidence, facts.evidence)
    &&& agrees(f3.price_deviation_bps, facts.price_deviation_bps)
    &&& agrees(f3.timeout_gap, facts.timeout_gap)
    &&& agrees(f3.reveal_window, facts.reveal_window)
    &&& lock_completes(f3.counterparty_lock, facts.counterparty_lock)
    &&& agrees(f3.asset_risk, facts.asset_risk)
    &&& agrees(f3.contract_pinned, facts.contract_pinned)
    &&& agrees(f3.collateral_bps, facts.collateral_bps)
}

pub open spec fn at_most(known: Option<u64>, cap: u64) -> Option<bool> {
    match known {
        Some(v) => Some(v <= cap),
        None => None,
    }
}

pub open spec fn at_least(known: Option<u64>, floor: u64) -> Option<bool> {
    match known {
        Some(v) => Some(v >= floor),
        None => None,
    }
}

/// A known entry for the period whose spend plus a known notional exceeds the cap.
pub open spec fn period_over(entry: PeriodSpent, period: u64, notional: Option<u64>, cap: u64) -> bool {
    entry.period == period && match (entry.spent, notional) {
        (Some(s), Some(n)) => s as int + n as int > cap as int,
        _ => false,
    }
}

pub open spec fn period_kleene(entries: Seq<PeriodSpent>, period: u64, notional: Option<u64>, cap: u64)
    -> Option<bool>
{
    if !(exists|i: int| 0 <= i < entries.len() && (#[trigger] entries[i]).period == period) {
        Some(false)
    } else if exists|i: int| 0 <= i < entries.len()
        && period_over(#[trigger] entries[i], period, notional, cap) {
        Some(false)
    } else if notional is Some && forall|i: int| 0 <= i < entries.len()
        && (#[trigger] entries[i]).period == period ==> entries[i].spent is Some {
        Some(true)
    } else {
        None
    }
}

pub open spec fn depth_at_least(depth: Option<u64>, d: u64) -> bool {
    match depth {
        Some(x) => x >= d,
        None => false,
    }
}

pub open spec fn depth_below(depth: Option<u64>, d: u64) -> bool {
    match depth {
        Some(x) => x < d,
        None => false,
    }
}

pub open spec fn finality_kleene(lock: Option<LockObs>, chain: [u8; 32], d: u64) -> Option<bool> {
    match lock {
        None => Some(true),
        Some(l) => if l.chain@ != chain@ {
            Some(true)
        } else if l.finalized == Some(true) || depth_at_least(l.depth, d) {
            Some(true)
        } else if l.finalized == Some(false) && depth_below(l.depth, d) {
            Some(false)
        } else {
            None
        },
    }
}

/// Strong Kleene semantics: `Some(b)` only when every completion evaluates to `b`.
pub open spec fn kleene(rule: &SwapRule, f3: &SwapFacts3) -> Option<bool>
    decreases rule,
{
    match rule {
        SwapRule::All(children) => {
            if exists|i: int| 0 <= i < children.len()
                && kleene(#[trigger] &children[i], f3) == Some(false) {
                Some(false)
            } else if forall|i: int| 0 <= i < children.len()
                ==> kleene(#[trigger] &children[i], f3) == Some(true) {
                Some(true)
            } else {
                None
            }
        },
        SwapRule::Any(children) => {
            if exists|i: int| 0 <= i < children.len()
                && kleene(#[trigger] &children[i], f3) == Some(true) {
                Some(true)
            } else if forall|i: int| 0 <= i < children.len()
                ==> kleene(#[trigger] &children[i], f3) == Some(false) {
                Some(false)
            } else {
                None
            }
        },
        SwapRule::ChainIn(set) => Some(id_in(set@, f3.give_chain) && id_in(set@, f3.take_chain)),
        SwapRule::AssetIn(set) => Some(id_in(set@, f3.give_asset) && id_in(set@, f3.take_asset)),
        SwapRule::CounterpartyIn(set) => match f3.counterparty {
            Some(id) => Some(id_in(set@, id)),
            None => None,
        },
        SwapRule::CounterpartyNotListed(hash) => match f3.list_check {
            Some(c) => Some(c.list_hash@ == hash@ && !c.listed),
            None => None,
        },
        SwapRule::NotionalAtMost(cap) => at_most(f3.notional, *cap),
        SwapRule::PeriodNotionalAtMost(period, cap) =>
            period_kleene(f3.period_spent@, *period, f3.notional, *cap),
        SwapRule::OpenSwapsAtMost(n) => at_most(f3.open_swaps, *n),
        SwapRule::EvidenceAtLeast(m) => match f3.evidence {
            Some(e) => Some(e >= *m),
            None => None,
        },
        SwapRule::PairIn(pairs) => Some(pair_in(pairs@, f3.give_asset, f3.take_asset)),
        SwapRule::PriceDeviationAtMost(bps) => at_most(f3.price_deviation_bps, *bps),
        SwapRule::TimeoutGapAtLeast(secs) => at_least(f3.timeout_gap, *secs),
        SwapRule::RevealWindowAtLeast(secs) => at_least(f3.reveal_window, *secs),
        SwapRule::FinalityAtLeast(chain, depth) => finality_kleene(f3.counterparty_lock, *chain, *depth),
        SwapRule::AssetRiskWithin(allowed) => match f3.asset_risk {
            Some(r) => Some(r & !*allowed == 0),
            None => None,
        },
        SwapRule::ContractPinned => f3.contract_pinned,
        SwapRule::CollateralAtLeast(bps) => at_least(f3.collateral_bps, *bps),
    }
}

// ---------------------------------------------------------------------------
// Soundness of the three-valued meaning
// ---------------------------------------------------------------------------

proof fn period_sound(f3: &SwapFacts3, facts: &SwapFacts, period: u64, cap: u64)
    requires completes(f3, facts),
    ensures
        period_kleene(f3.period_spent@, period, f3.notional, cap) == Some(true)
            ==> period_ok(facts.period_spent@, period, facts.notional, cap),
        period_kleene(f3.period_spent@, period, f3.notional, cap) == Some(false)
            ==> !period_ok(facts.period_spent@, period, facts.notional, cap),
{
    let e3 = f3.period_spent@;
    let e = facts.period_spent@;
    if !(exists|i: int| 0 <= i < e3.len() && (#[trigger] e3[i]).period == period) {
        if exists|i: int| 0 <= i < e.len() && (#[trigger] e[i]).period == period {
            let i = choose|i: int| 0 <= i < e.len() && (#[trigger] e[i]).period == period;
            assert(f3.period_spent[i].period == facts.period_spent[i].period);
            assert(e3[i].period == period);
        }
    } else {
        let k = choose|i: int| 0 <= i < e3.len() && (#[trigger] e3[i]).period == period;
        assert(f3.period_spent[k].period == facts.period_spent[k].period);
        assert(e[k].period == period);
        if exists|i: int| 0 <= i < e3.len() && period_over(#[trigger] e3[i], period, f3.notional, cap) {
            let i = choose|i: int| 0 <= i < e3.len() && period_over(#[trigger] e3[i], period, f3.notional, cap);
            assert(f3.period_spent[i].period == facts.period_spent[i].period
                && agrees(f3.period_spent[i].spent, facts.period_spent[i].spent));
            assert(e[i].period == period);
        } else if f3.notional is Some && forall|i: int| 0 <= i < e3.len()
            && (#[trigger] e3[i]).period == period ==> e3[i].spent is Some {
            assert forall|i: int| 0 <= i < e.len() && (#[trigger] e[i]).period == period implies
                e[i].spent as int + facts.notional as int <= cap as int by {
                assert(f3.period_spent[i].period == facts.period_spent[i].period
                    && agrees(f3.period_spent[i].spent, facts.period_spent[i].spent));
                assert(e3[i].period == period);
                assert(!period_over(e3[i], period, f3.notional, cap));
            }
        }
    }
}

/// Soundness: a known three-valued result holds for every completion of the facts.
pub proof fn kleene_sound(rule: &SwapRule, f3: &SwapFacts3, facts: &SwapFacts)
    requires completes(f3, facts),
    ensures
        kleene(rule, f3) == Some(true) ==> satisfies(rule, facts),
        kleene(rule, f3) == Some(false) ==> !satisfies(rule, facts),
    decreases rule,
{
    match rule {
        SwapRule::All(children) => {
            assert forall|i: int| 0 <= i < children.len() implies
                (kleene(#[trigger] &children[i], f3) == Some(true) ==> satisfies(&children[i], facts))
                && (kleene(&children[i], f3) == Some(false) ==> !satisfies(&children[i], facts)) by {
                kleene_sound(&children[i], f3, facts);
            }
        },
        SwapRule::Any(children) => {
            assert forall|i: int| 0 <= i < children.len() implies
                (kleene(#[trigger] &children[i], f3) == Some(true) ==> satisfies(&children[i], facts))
                && (kleene(&children[i], f3) == Some(false) ==> !satisfies(&children[i], facts)) by {
                kleene_sound(&children[i], f3, facts);
            }
        },
        SwapRule::PeriodNotionalAtMost(period, cap) => {
            period_sound(f3, facts, *period, *cap);
        },
        _ => {},
    }
}

/// Allow and Deny are each correct for every way of filling in the unknowns.
pub proof fn decision_sound(rule: &SwapRule, f3: &SwapFacts3)
    ensures
        kleene(rule, f3) == Some(true) ==> forall|facts: SwapFacts|
            completes(f3, &facts) ==> #[trigger] satisfies(rule, &facts),
        kleene(rule, f3) == Some(false) ==> forall|facts: SwapFacts|
            completes(f3, &facts) ==> !#[trigger] satisfies(rule, &facts),
{
    assert forall|facts: SwapFacts| completes(f3, &facts) implies
        (kleene(rule, f3) == Some(true) ==> #[trigger] satisfies(rule, &facts))
        && (kleene(rule, f3) == Some(false) ==> !satisfies(rule, &facts)) by {
        kleene_sound(rule, f3, &facts);
    }
}

// ---------------------------------------------------------------------------
// Executable evaluation
// ---------------------------------------------------------------------------

fn bytes_equal(a: &[u8; 32], b: &[u8; 32]) -> (result: bool)
    ensures result == (a@ == b@),
{
    let mut i: usize = 0;
    while i < 32
        invariant
            i <= 32,
            forall|j: int| 0 <= j < i ==> a[j] == b[j],
        decreases 32 - i,
    {
        if a[i] != b[i] {
            return false;
        }
        i += 1;
    }
    assert(a@ =~= b@);
    true
}

fn id_in_exec(set: &Vec<[u8; 32]>, id: &[u8; 32]) -> (result: bool)
    ensures result == id_in(set@, *id),
{
    let mut i: usize = 0;
    while i < set.len()
        invariant
            i <= set.len(),
            forall|j: int| 0 <= j < i ==> (#[trigger] set@[j])@ != id@,
        decreases set.len() - i,
    {
        if bytes_equal(&set[i], id) {
            assert(set@[i as int]@ == id@);
            return true;
        }
        i += 1;
    }
    false
}

fn pair_in_exec(pairs: &Vec<Pair>, give: &[u8; 32], take: &[u8; 32]) -> (result: bool)
    ensures result == pair_in(pairs@, *give, *take),
{
    let mut i: usize = 0;
    while i < pairs.len()
        invariant
            i <= pairs.len(),
            forall|j: int| 0 <= j < i ==> !((#[trigger] pairs@[j]).give@ == give@ && pairs@[j].take@ == take@),
        decreases pairs.len() - i,
    {
        if bytes_equal(&pairs[i].give, give) && bytes_equal(&pairs[i].take, take) {
            assert(pairs@[i as int].give@ == give@ && pairs@[i as int].take@ == take@);
            return true;
        }
        i += 1;
    }
    false
}

fn at_most_exec(known: Option<u64>, cap: u64) -> (result: Option<bool>)
    ensures result == at_most(known, cap),
{
    match known {
        Some(v) => Some(v <= cap),
        None => None,
    }
}

fn at_least_exec(known: Option<u64>, floor: u64) -> (result: Option<bool>)
    ensures result == at_least(known, floor),
{
    match known {
        Some(v) => Some(v >= floor),
        None => None,
    }
}

fn period_exec(entries: &Vec<PeriodSpent>, period: u64, notional: Option<u64>, cap: u64)
    -> (result: Option<bool>)
    ensures result == period_kleene(entries@, period, notional, cap),
{
    let mut found = false;
    let mut unknown = false;
    let mut i: usize = 0;
    while i < entries.len()
        invariant
            i <= entries.len(),
            found == exists|j: int| 0 <= j < i && (#[trigger] entries@[j]).period == period,
            unknown == exists|j: int| 0 <= j < i
                && (#[trigger] entries@[j]).period == period && entries@[j].spent is None,
            forall|j: int| 0 <= j < i ==> !period_over(#[trigger] entries@[j], period, notional, cap),
        decreases entries.len() - i,
    {
        let e = entries[i];
        if e.period == period {
            found = true;
            match (e.spent, notional) {
                (Some(s), Some(n)) => {
                    if (s as u128) + (n as u128) > cap as u128 {
                        assert(period_over(entries@[i as int], period, notional, cap));
                        return Some(false);
                    }
                },
                (None, _) => {
                    unknown = true;
                },
                _ => {},
            }
        }
        i += 1;
    }
    if !found {
        Some(false)
    } else if unknown {
        None
    } else {
        match notional {
            None => None,
            Some(_) => {
                assert forall|j: int| 0 <= j < entries.len()
                    && (#[trigger] entries@[j]).period == period implies entries@[j].spent is Some by {
                    if entries@[j].spent is None {
                        assert(exists|k: int| 0 <= k < i
                            && (#[trigger] entries@[k]).period == period && entries@[k].spent is None);
                    }
                }
                Some(true)
            },
        }
    }
}

fn finality_exec(lock: &Option<LockObs>, chain: &[u8; 32], d: u64) -> (result: Option<bool>)
    ensures result == finality_kleene(*lock, *chain, d),
{
    match lock {
        None => Some(true),
        Some(l) => {
            if !bytes_equal(&l.chain, chain) {
                Some(true)
            } else {
                let fin_true = match l.finalized {
                    Some(f) => f,
                    None => false,
                };
                let fin_false = match l.finalized {
                    Some(f) => !f,
                    None => false,
                };
                let deep = match l.depth {
                    Some(x) => x >= d,
                    None => false,
                };
                let shallow = match l.depth {
                    Some(x) => x < d,
                    None => false,
                };
                if fin_true || deep {
                    Some(true)
                } else if fin_false && shallow {
                    Some(false)
                } else {
                    None
                }
            }
        },
    }
}

/// Executable strong Kleene evaluation; equal to `kleene` for every rule and facts.
#[verifier::loop_isolation(false)]
pub fn evaluate3(rule: &SwapRule, f3: &SwapFacts3) -> (result: Option<bool>)
    ensures result == kleene(rule, f3),
    decreases rule,
{
    match rule {
        SwapRule::All(children) => {
            let mut unknown = false;
            let mut i: usize = 0;
            while i < children.len()
                invariant
                    i <= children.len(),
                    forall|j: int| 0 <= j < i ==> kleene(#[trigger] &children[j], f3) != Some(false),
                    unknown == exists|j: int| 0 <= j < i && kleene(#[trigger] &children[j], f3) is None,
                decreases children.len() - i,
            {
                match evaluate3(&children[i], f3) {
                    Some(false) => return Some(false),
                    None => unknown = true,
                    Some(true) => {},
                }
                i += 1;
            }
            if unknown {
                None
            } else {
                assert forall|j: int| 0 <= j < children.len() implies
                    kleene(#[trigger] &children[j], f3) == Some(true) by {
                    if kleene(&children[j], f3) is None {
                        assert(exists|k: int| 0 <= k < i && kleene(#[trigger] &children[k], f3) is None);
                    }
                }
                Some(true)
            }
        },
        SwapRule::Any(children) => {
            let mut unknown = false;
            let mut i: usize = 0;
            while i < children.len()
                invariant
                    i <= children.len(),
                    forall|j: int| 0 <= j < i ==> kleene(#[trigger] &children[j], f3) != Some(true),
                    unknown == exists|j: int| 0 <= j < i && kleene(#[trigger] &children[j], f3) is None,
                decreases children.len() - i,
            {
                match evaluate3(&children[i], f3) {
                    Some(true) => return Some(true),
                    None => unknown = true,
                    Some(false) => {},
                }
                i += 1;
            }
            if unknown {
                None
            } else {
                assert forall|j: int| 0 <= j < children.len() implies
                    kleene(#[trigger] &children[j], f3) == Some(false) by {
                    if kleene(&children[j], f3) is None {
                        assert(exists|k: int| 0 <= k < i && kleene(#[trigger] &children[k], f3) is None);
                    }
                }
                Some(false)
            }
        },
        SwapRule::ChainIn(set) => Some(id_in_exec(set, &f3.give_chain) && id_in_exec(set, &f3.take_chain)),
        SwapRule::AssetIn(set) => Some(id_in_exec(set, &f3.give_asset) && id_in_exec(set, &f3.take_asset)),
        SwapRule::CounterpartyIn(set) => match &f3.counterparty {
            Some(id) => Some(id_in_exec(set, id)),
            None => None,
        },
        SwapRule::CounterpartyNotListed(hash) => match &f3.list_check {
            Some(c) => Some(bytes_equal(&c.list_hash, hash) && !c.listed),
            None => None,
        },
        SwapRule::NotionalAtMost(cap) => at_most_exec(f3.notional, *cap),
        SwapRule::PeriodNotionalAtMost(period, cap) =>
            period_exec(&f3.period_spent, *period, f3.notional, *cap),
        SwapRule::OpenSwapsAtMost(n) => at_most_exec(f3.open_swaps, *n),
        SwapRule::EvidenceAtLeast(m) => match f3.evidence {
            Some(e) => Some(e >= *m),
            None => None,
        },
        SwapRule::PairIn(pairs) => Some(pair_in_exec(pairs, &f3.give_asset, &f3.take_asset)),
        SwapRule::PriceDeviationAtMost(bps) => at_most_exec(f3.price_deviation_bps, *bps),
        SwapRule::TimeoutGapAtLeast(secs) => at_least_exec(f3.timeout_gap, *secs),
        SwapRule::RevealWindowAtLeast(secs) => at_least_exec(f3.reveal_window, *secs),
        SwapRule::FinalityAtLeast(chain, depth) => finality_exec(&f3.counterparty_lock, chain, *depth),
        SwapRule::AssetRiskWithin(allowed) => match f3.asset_risk {
            Some(r) => Some(r & !*allowed == 0),
            None => None,
        },
        SwapRule::ContractPinned => f3.contract_pinned,
        SwapRule::CollateralAtLeast(bps) => at_least_exec(f3.collateral_bps, *bps),
    }
}

/// Allow only when the policy holds for every completion; Deny only when it fails
/// for every completion; otherwise Ask. Only Allow may authorize an entry action.
pub fn decide(rule: &SwapRule, f3: &SwapFacts3) -> (result: Decision)
    ensures
        result == Decision::Allow <==> kleene(rule, f3) == Some(true),
        result == Decision::Deny <==> kleene(rule, f3) == Some(false),
{
    match evaluate3(rule, f3) {
        Some(true) => Decision::Allow,
        Some(false) => Decision::Deny,
        None => Decision::Ask,
    }
}

// ---------------------------------------------------------------------------
// Timeout arithmetic (spec 7.3: S11 and S12)
// ---------------------------------------------------------------------------

/// Conservative clock bounds of one chain profile.
#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
pub struct ClockBounds {
    pub min_block_secs: u64,
    pub max_block_secs: u64,
    /// How far the chain clock can run ahead of real time.
    pub max_lead_secs: u64,
    /// How far the chain clock can lag behind real time.
    pub max_lag_secs: u64,
}

/// An absolute timelock: the first height, or the first chain time, at which the
/// refund is valid.
#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "serde", serde(rename_all = "snake_case"))]
pub enum Timelock {
    Height(u64),
    Time(u64),
}

/// The observed chain tip and the policy signer's real time (Unix seconds).
#[derive(Clone, Copy)]
#[cfg_attr(feature = "serde", derive(Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
pub struct ChainNow {
    pub tip_height: u64,
    pub now_real: u64,
}

/// The refund is certainly not valid yet.
pub open spec fn pending(t: Timelock, now: ChainNow, b: ClockBounds) -> bool {
    match t {
        Timelock::Height(h) => h > now.tip_height,
        Timelock::Time(tt) => tt as int > now.now_real as int + b.max_lead_secs as int,
    }
}

pub open spec fn earliest_real(t: Timelock, now: ChainNow, b: ClockBounds) -> int {
    match t {
        Timelock::Height(h) => now.now_real as int
            + (h as int - now.tip_height as int - 1) * b.min_block_secs as int,
        Timelock::Time(tt) => tt as int - b.max_lead_secs as int,
    }
}

pub open spec fn latest_real(t: Timelock, now: ChainNow, b: ClockBounds) -> int {
    match t {
        Timelock::Height(h) => now.now_real as int
            + (h as int - now.tip_height as int) * b.max_block_secs as int,
        Timelock::Time(tt) => tt as int + b.max_lag_secs as int,
    }
}

pub open spec fn clamp_u64(x: int) -> int {
    if x < 0 { 0 } else if x > u64::MAX as int { u64::MAX as int } else { x }
}

pub open spec fn s11_spec(
    ta: Timelock, now_a: ChainNow, ba: ClockBounds,
    tb: Timelock, now_b: ChainNow, bb: ClockBounds,
    d_observe: u64, d_confirm: u64, d_margin: u64,
) -> bool {
    &&& pending(ta, now_a, ba)
    &&& pending(tb, now_b, bb)
    &&& earliest_real(ta, now_a, ba) - latest_real(tb, now_b, bb)
        >= d_observe as int + d_confirm as int + d_margin as int
}

pub open spec fn s12_spec(tb: Timelock, now_b: ChainNow, bb: ClockBounds, d_confirm: u64, d_margin: u64) -> bool {
    &&& pending(tb, now_b, bb)
    &&& now_b.now_real as int + d_confirm as int + d_margin as int <= earliest_real(tb, now_b, bb)
}

pub open spec fn gap_spec(ta: Timelock, now_a: ChainNow, ba: ClockBounds, tb: Timelock, now_b: ChainNow, bb: ClockBounds) -> int {
    if pending(ta, now_a, ba) && pending(tb, now_b, bb) {
        clamp_u64(earliest_real(ta, now_a, ba) - latest_real(tb, now_b, bb))
    } else {
        0
    }
}

pub open spec fn window_spec(tb: Timelock, now_b: ChainNow, bb: ClockBounds, d_confirm: u64) -> int {
    if pending(tb, now_b, bb) {
        clamp_u64(earliest_real(tb, now_b, bb) - now_b.now_real as int - d_confirm as int)
    } else {
        0
    }
}

pub fn pending_exec(t: Timelock, now: ChainNow, b: ClockBounds) -> (result: bool)
    ensures result == pending(t, now, b),
{
    match t {
        Timelock::Height(h) => h > now.tip_height,
        Timelock::Time(tt) => (tt as u128) > (now.now_real as u128) + (b.max_lead_secs as u128),
    }
}

proof fn lemma_mul_bound(x: u64, y: u64)
    ensures (x as int) * (y as int) <= (u64::MAX as int) * (u64::MAX as int),
            (x as int) * (y as int) >= 0,
{
    assert((x as int) * (y as int) <= (u64::MAX as int) * (u64::MAX as int)) by (nonlinear_arith)
        requires x as int <= u64::MAX as int, y as int <= u64::MAX as int, x as int >= 0, y as int >= 0;
    assert((x as int) * (y as int) >= 0) by (nonlinear_arith)
        requires x as int >= 0, y as int >= 0;
}

/// Requires a pending timelock. Never overflows: every term is below 2^128.
pub fn earliest_exec(t: Timelock, now: ChainNow, b: ClockBounds) -> (result: u128)
    requires pending(t, now, b),
    ensures result as int == earliest_real(t, now, b),
{
    match t {
        Timelock::Height(h) => {
            let blocks = h - now.tip_height - 1;
            proof { lemma_mul_bound(blocks, b.min_block_secs); }
            (now.now_real as u128) + (blocks as u128) * (b.min_block_secs as u128)
        },
        Timelock::Time(tt) => (tt - b.max_lead_secs) as u128,
    }
}

pub fn latest_exec(t: Timelock, now: ChainNow, b: ClockBounds) -> (result: u128)
    requires pending(t, now, b),
    ensures result as int == latest_real(t, now, b),
{
    match t {
        Timelock::Height(h) => {
            let blocks = h - now.tip_height;
            proof { lemma_mul_bound(blocks, b.max_block_secs); }
            (now.now_real as u128) + (blocks as u128) * (b.max_block_secs as u128)
        },
        Timelock::Time(tt) => (tt as u128) + (b.max_lag_secs as u128),
    }
}

fn clamp_exec(earlier: u128, later: u128) -> (result: u64)
    ensures result as int == clamp_u64(later as int - earlier as int),
{
    if later <= earlier {
        0
    } else if later - earlier > u64::MAX as u128 {
        u64::MAX
    } else {
        (later - earlier) as u64
    }
}

/// Fact `timeout_gap`: earliest refund of leg A minus latest refund of leg B,
/// clamped to `[0, u64::MAX]`; 0 when either timelock is not pending.
pub fn timeout_gap(ta: Timelock, now_a: ChainNow, ba: ClockBounds, tb: Timelock, now_b: ChainNow, bb: ClockBounds)
    -> (result: u64)
    ensures result as int == gap_spec(ta, now_a, ba, tb, now_b, bb),
{
    if pending_exec(ta, now_a, ba) && pending_exec(tb, now_b, bb) {
        clamp_exec(latest_exec(tb, now_b, bb), earliest_exec(ta, now_a, ba))
    } else {
        0
    }
}

/// Fact `reveal_window`: time left before the reveal deadline of S12, without
/// the policy margin; 0 when the timelock is not pending.
pub fn reveal_window(tb: Timelock, now_b: ChainNow, bb: ClockBounds, d_confirm: u64) -> (result: u64)
    ensures result as int == window_spec(tb, now_b, bb, d_confirm),
{
    if pending_exec(tb, now_b, bb) {
        clamp_exec((now_b.now_real as u128) + (d_confirm as u128), earliest_exec(tb, now_b, bb))
    } else {
        0
    }
}

/// S11: the responder's ordering and gap requirement.
pub fn s11_holds(
    ta: Timelock, now_a: ChainNow, ba: ClockBounds,
    tb: Timelock, now_b: ChainNow, bb: ClockBounds,
    d_observe: u64, d_confirm: u64, d_margin: u64,
) -> (result: bool)
    ensures result == s11_spec(ta, now_a, ba, tb, now_b, bb, d_observe, d_confirm, d_margin),
{
    if !(pending_exec(ta, now_a, ba) && pending_exec(tb, now_b, bb)) {
        return false;
    }
    let need = (d_observe as u128) + (d_confirm as u128) + (d_margin as u128);
    let earliest_a = earliest_exec(ta, now_a, ba);
    let latest_b = latest_exec(tb, now_b, bb);
    earliest_a >= latest_b && earliest_a - latest_b >= need
}

/// S12: the initiator's reveal deadline.
pub fn s12_holds(tb: Timelock, now_b: ChainNow, bb: ClockBounds, d_confirm: u64, d_margin: u64) -> (result: bool)
    ensures result == s12_spec(tb, now_b, bb, d_confirm, d_margin),
{
    if !pending_exec(tb, now_b, bb) {
        return false;
    }
    (now_b.now_real as u128) + (d_confirm as u128) + (d_margin as u128) <= earliest_exec(tb, now_b, bb)
}

// ---------------------------------------------------------------------------
// Clock model: the bounds are conservative
// ---------------------------------------------------------------------------

/// `bt(k)` is the real time of the k-th block after the observed tip.
pub open spec fn block_times_ok(bt: spec_fn(int) -> int, now: ChainNow, b: ClockBounds) -> bool {
    &&& 0 <= bt(1) - now.now_real as int <= b.max_block_secs as int
    &&& forall|k: int| k >= 1 ==> b.min_block_secs as int <= #[trigger] bt(k + 1) - bt(k)
        && bt(k + 1) - bt(k) <= b.max_block_secs as int
}

/// `c(r)` is the chain clock (for Bitcoin, the median time past) at real time `r`.
pub open spec fn chain_clock_ok(c: spec_fn(int) -> int, b: ClockBounds) -> bool {
    forall|r: int| r - b.max_lag_secs as int <= #[trigger] c(r) && c(r) <= r + b.max_lead_secs as int
}

pub open spec fn model_ok(now: ChainNow, b: ClockBounds, bt: spec_fn(int) -> int, c: spec_fn(int) -> int) -> bool {
    block_times_ok(bt, now, b) && chain_clock_ok(c, b)
}

/// The refund of timelock `t` is valid at real time `r` in the model.
pub open spec fn refund_valid(t: Timelock, now: ChainNow, bt: spec_fn(int) -> int, c: spec_fn(int) -> int, r: int) -> bool {
    match t {
        Timelock::Height(h) => r >= bt(h as int - now.tip_height as int),
        Timelock::Time(tt) => c(r) >= tt as int,
    }
}

pub proof fn lemma_block_time_bounds(bt: spec_fn(int) -> int, now: ChainNow, b: ClockBounds, n: int)
    requires
        block_times_ok(bt, now, b),
        n >= 1,
    ensures
        now.now_real as int + (n - 1) * b.min_block_secs as int <= bt(n),
        bt(n) <= now.now_real as int + n * b.max_block_secs as int,
    decreases n,
{
    if n > 1 {
        lemma_block_time_bounds(bt, now, b, n - 1);
        let k = n - 1;
        assert(b.min_block_secs as int <= bt(k + 1) - bt(k) && bt(k + 1) - bt(k) <= b.max_block_secs as int);
        assert((n - 1) * b.min_block_secs as int == (n - 2) * b.min_block_secs as int + b.min_block_secs as int)
            by (nonlinear_arith);
        assert(n * b.max_block_secs as int == (n - 1) * b.max_block_secs as int + b.max_block_secs as int)
            by (nonlinear_arith);
    } else {
        assert((n - 1) * b.min_block_secs as int == 0) by (nonlinear_arith) requires n == 1;
        assert(n * b.max_block_secs as int == b.max_block_secs as int) by (nonlinear_arith) requires n == 1;
    }
}

/// The model is not vacuous: for consistent bounds, a chain that meets them exists.
pub proof fn model_is_satisfiable(now: ChainNow, b: ClockBounds)
    requires b.min_block_secs <= b.max_block_secs,
    ensures exists|bt: spec_fn(int) -> int, c: spec_fn(int) -> int| model_ok(now, b, bt, c),
{
    let m = b.max_block_secs as int;
    let bt = |k: int| now.now_real as int + k * m;
    let c = |r: int| r;
    assert forall|k: int| k >= 1 implies b.min_block_secs as int <= #[trigger] bt(k + 1) - bt(k)
        && bt(k + 1) - bt(k) <= m by {
        assert((k + 1) * m == k * m + m) by (nonlinear_arith);
    }
    assert(1 * m == m);
    assert(model_ok(now, b, bt, c));
}

/// A refund is never valid before `earliest_real`.
pub proof fn earliest_is_conservative(
    t: Timelock, now: ChainNow, b: ClockBounds, bt: spec_fn(int) -> int, c: spec_fn(int) -> int, r: int,
)
    requires
        pending(t, now, b),
        model_ok(now, b, bt, c),
        refund_valid(t, now, bt, c, r),
    ensures r >= earliest_real(t, now, b),
{
    match t {
        Timelock::Height(h) => {
            lemma_block_time_bounds(bt, now, b, h as int - now.tip_height as int);
        },
        Timelock::Time(tt) => {
            assert(c(r) <= r + b.max_lead_secs as int);
        },
    }
}

/// A refund is always valid from `latest_real` on.
pub proof fn latest_is_conservative(
    t: Timelock, now: ChainNow, b: ClockBounds, bt: spec_fn(int) -> int, c: spec_fn(int) -> int, r: int,
)
    requires
        pending(t, now, b),
        model_ok(now, b, bt, c),
        r >= latest_real(t, now, b),
    ensures refund_valid(t, now, bt, c, r),
{
    match t {
        Timelock::Height(h) => {
            lemma_block_time_bounds(bt, now, b, h as int - now.tip_height as int);
        },
        Timelock::Time(tt) => {
            assert(r - b.max_lag_secs as int <= c(r));
        },
    }
}

/// S11 soundness: whenever leg A is refundable, leg B has been refundable for at
/// least `d_observe + d_confirm + d_margin` seconds.
pub proof fn s11_sound(
    ta: Timelock, now_a: ChainNow, ba: ClockBounds, bt_a: spec_fn(int) -> int, c_a: spec_fn(int) -> int,
    tb: Timelock, now_b: ChainNow, bb: ClockBounds, bt_b: spec_fn(int) -> int, c_b: spec_fn(int) -> int,
    d_observe: u64, d_confirm: u64, d_margin: u64, ra: int,
)
    requires
        s11_spec(ta, now_a, ba, tb, now_b, bb, d_observe, d_confirm, d_margin),
        model_ok(now_a, ba, bt_a, c_a),
        model_ok(now_b, bb, bt_b, c_b),
        refund_valid(ta, now_a, bt_a, c_a, ra),
    ensures
        refund_valid(tb, now_b, bt_b, c_b, ra - d_observe as int - d_confirm as int - d_margin as int),
{
    earliest_is_conservative(ta, now_a, ba, bt_a, c_a, ra);
    latest_is_conservative(tb, now_b, bb, bt_b, c_b,
        ra - d_observe as int - d_confirm as int - d_margin as int);
}

/// S12 soundness: leg B is not refundable before the claim has had
/// `d_confirm + d_margin` seconds to reach finality.
pub proof fn s12_sound(
    tb: Timelock, now_b: ChainNow, bb: ClockBounds, bt: spec_fn(int) -> int, c: spec_fn(int) -> int,
    d_confirm: u64, d_margin: u64, r: int,
)
    requires
        s12_spec(tb, now_b, bb, d_confirm, d_margin),
        model_ok(now_b, bb, bt, c),
        refund_valid(tb, now_b, bt, c, r),
    ensures r >= now_b.now_real as int + d_confirm as int + d_margin as int,
{
    earliest_is_conservative(tb, now_b, bb, bt, c, r);
}

} // verus!
