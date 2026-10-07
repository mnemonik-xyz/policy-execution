//! Native checks of the verified evaluator and timeout arithmetic. The Verus proofs
//! cover every input; these tests pin concrete behaviour and the serde encoding.
use warrant_swap_verified::*;

fn id(b: u8) -> [u8; 32] {
    [b; 32]
}

fn facts() -> SwapFacts3 {
    SwapFacts3 {
        give_chain: id(1),
        take_chain: id(2),
        give_asset: id(11),
        take_asset: id(12),
        counterparty: Some(id(21)),
        list_check: Some(ListCheck { list_hash: id(31), listed: false }),
        notional: Some(40_000),
        period_spent: vec![PeriodSpent { period: 86_400, spent: Some(100_000) }],
        open_swaps: Some(2),
        evidence: Some(2),
        price_deviation_bps: Some(30),
        timeout_gap: Some(9_000),
        reveal_window: Some(4_000),
        counterparty_lock: Some(LockObs { chain: id(1), depth: Some(3), finalized: Some(false) }),
        asset_risk: Some(1),
        contract_pinned: Some(true),
        collateral_bps: Some(0),
    }
}

fn policy() -> SwapRule {
    SwapRule::All(vec![
        SwapRule::PairIn(vec![Pair { give: id(11), take: id(12) }]),
        SwapRule::ChainIn(vec![id(1), id(2)]),
        SwapRule::NotionalAtMost(50_000),
        SwapRule::PeriodNotionalAtMost(86_400, 200_000),
        SwapRule::PriceDeviationAtMost(50),
        SwapRule::TimeoutGapAtLeast(7_200),
        SwapRule::FinalityAtLeast(id(1), 3),
        SwapRule::EvidenceAtLeast(2),
        SwapRule::AssetRiskWithin(1),
        SwapRule::ContractPinned,
        SwapRule::Any(vec![
            SwapRule::CounterpartyIn(vec![id(21)]),
            SwapRule::NotionalAtMost(1_000),
        ]),
    ])
}

#[test]
fn example_policy_allows() {
    assert_eq!(decide(&policy(), &facts()), Decision::Allow);
}

#[test]
fn each_known_violation_denies() {
    let cases: Vec<Box<dyn Fn(&mut SwapFacts3)>> = vec![
        Box::new(|f| f.take_asset = id(99)),
        Box::new(|f| f.take_chain = id(99)),
        Box::new(|f| f.notional = Some(50_001)),
        Box::new(|f| f.period_spent[0].spent = Some(160_001)),
        Box::new(|f| f.period_spent[0].period = 3_600),
        Box::new(|f| f.price_deviation_bps = Some(51)),
        Box::new(|f| f.timeout_gap = Some(7_199)),
        Box::new(|f| f.counterparty_lock = Some(LockObs { chain: id(1), depth: Some(2), finalized: Some(false) })),
        Box::new(|f| f.evidence = Some(1)),
        Box::new(|f| f.asset_risk = Some(0b11)),
        Box::new(|f| f.contract_pinned = Some(false)),
    ];
    for (i, mutate) in cases.iter().enumerate() {
        let mut f = facts();
        mutate(&mut f);
        assert_eq!(decide(&policy(), &f), Decision::Deny, "case {i}");
    }
}

#[test]
fn unknowns_ask_and_never_allow() {
    let cases: Vec<Box<dyn Fn(&mut SwapFacts3)>> = vec![
        Box::new(|f| f.notional = None),
        Box::new(|f| f.period_spent[0].spent = None),
        Box::new(|f| f.price_deviation_bps = None),
        Box::new(|f| f.timeout_gap = None),
        Box::new(|f| f.counterparty_lock = Some(LockObs { chain: id(1), depth: None, finalized: None })),
        Box::new(|f| f.evidence = None),
        Box::new(|f| f.asset_risk = None),
        Box::new(|f| f.contract_pinned = None),
    ];
    for (i, mutate) in cases.iter().enumerate() {
        let mut f = facts();
        mutate(&mut f);
        assert_eq!(decide(&policy(), &f), Decision::Ask, "case {i}");
    }
}

#[test]
fn unknown_counterparty_only_small_trades() {
    let mut f = facts();
    f.counterparty = None;
    assert_eq!(decide(&policy(), &f), Decision::Ask);
    f.counterparty = Some(id(77));
    assert_eq!(decide(&policy(), &f), Decision::Deny);
    f.notional = Some(900);
    assert_eq!(decide(&policy(), &f), Decision::Allow);
}

#[test]
fn finality_rules() {
    let rule = SwapRule::FinalityAtLeast(id(1), 6);
    let mut f = facts();
    f.counterparty_lock = None;
    assert_eq!(decide(&rule, &f), Decision::Allow, "no counterparty lock");
    f.counterparty_lock = Some(LockObs { chain: id(2), depth: Some(0), finalized: Some(false) });
    assert_eq!(decide(&rule, &f), Decision::Allow, "other chain");
    f.counterparty_lock = Some(LockObs { chain: id(1), depth: Some(1), finalized: Some(true) });
    assert_eq!(decide(&rule, &f), Decision::Allow, "finalized");
    f.counterparty_lock = Some(LockObs { chain: id(1), depth: Some(5), finalized: None });
    assert_eq!(decide(&rule, &f), Decision::Ask, "shallow, finality unknown");
    f.counterparty_lock = Some(LockObs { chain: id(1), depth: Some(5), finalized: Some(false) });
    assert_eq!(decide(&rule, &f), Decision::Deny, "shallow, not finalized");
}

#[test]
fn period_limit_with_duplicate_entries_is_conservative() {
    let rule = SwapRule::PeriodNotionalAtMost(86_400, 200_000);
    let mut f = facts();
    f.period_spent = vec![
        PeriodSpent { period: 86_400, spent: Some(0) },
        PeriodSpent { period: 86_400, spent: Some(170_000) },
    ];
    assert_eq!(decide(&rule, &f), Decision::Deny);
    f.period_spent = vec![];
    assert_eq!(decide(&rule, &f), Decision::Deny, "untracked period");
    f.period_spent = vec![PeriodSpent { period: 86_400, spent: Some(u64::MAX) }];
    f.notional = Some(u64::MAX);
    assert_eq!(decide(&rule, &f), Decision::Deny, "no overflow");
}

#[test]
fn not_listed_needs_the_named_snapshot() {
    let rule = SwapRule::CounterpartyNotListed(id(31));
    let mut f = facts();
    assert_eq!(decide(&rule, &f), Decision::Allow);
    f.list_check = Some(ListCheck { list_hash: id(31), listed: true });
    assert_eq!(decide(&rule, &f), Decision::Deny);
    f.list_check = Some(ListCheck { list_hash: id(32), listed: false });
    assert_eq!(decide(&rule, &f), Decision::Deny);
    f.list_check = None;
    assert_eq!(decide(&rule, &f), Decision::Ask);
}

#[test]
fn empty_combinators() {
    assert_eq!(decide(&SwapRule::All(vec![]), &facts()), Decision::Allow);
    assert_eq!(decide(&SwapRule::Any(vec![]), &facts()), Decision::Deny);
}

const BTC: ClockBounds = ClockBounds { min_block_secs: 60, max_block_secs: 3_600, max_lead_secs: 7_200, max_lag_secs: 3_600 };
const EVM: ClockBounds = ClockBounds { min_block_secs: 12, max_block_secs: 12, max_lead_secs: 15, max_lag_secs: 15 };

#[test]
fn clock_bounds() {
    let now = ChainNow { tip_height: 100, now_real: 1_000_000 };
    let t = Timelock::Height(110);
    assert!(pending_exec(t, now, BTC));
    assert_eq!(earliest_exec(t, now, BTC), 1_000_000 + 9 * 60);
    assert_eq!(latest_exec(t, now, BTC), 1_000_000 + 10 * 3_600);
    let t = Timelock::Time(1_010_000);
    assert!(pending_exec(t, now, BTC));
    assert_eq!(earliest_exec(t, now, BTC), 1_010_000 - 7_200);
    assert_eq!(latest_exec(t, now, BTC), 1_010_000 + 3_600);
    assert!(!pending_exec(Timelock::Height(100), now, BTC));
    assert!(!pending_exec(Timelock::Time(1_007_200), now, BTC));
}

#[test]
fn s11_gap_and_s12_deadline() {
    // Leg A on Bitcoin, 144 blocks; leg B on an EVM chain, 4 hours.
    let now_a = ChainNow { tip_height: 800_000, now_real: 1_700_000_000 };
    let now_b = ChainNow { tip_height: 0, now_real: 1_700_000_000 };
    let ta = Timelock::Height(800_144);
    let tb = Timelock::Time(1_700_000_000 + 4 * 3_600);
    // Earliest A: 143 blocks of 60 s = 8_580 s; latest B: 14_400 + 15 s. Gap is negative.
    assert_eq!(timeout_gap(ta, now_a, BTC, tb, now_b, EVM), 0);
    assert!(!s11_holds(ta, now_a, BTC, tb, now_b, EVM, 600, 3_600, 600));
    // With a 9-minute floor on the Bitcoin block interval, the gap is wide enough.
    let btc = ClockBounds { min_block_secs: 540, ..BTC };
    let gap = timeout_gap(ta, now_a, btc, tb, now_b, EVM);
    assert_eq!(gap, 143 * 540 - (4 * 3_600 + 15));
    assert!(s11_holds(ta, now_a, btc, tb, now_b, EVM, 600, 3_600, 600));
    assert!(!s11_holds(ta, now_a, btc, tb, now_b, EVM, 600, 3_600, gap));
    // S12 on leg B: the claim needs 15 minutes plus a 10-minute margin.
    assert!(s12_holds(tb, now_b, EVM, 900, 600));
    let late = ChainNow { tip_height: 0, now_real: 1_700_000_000 + 4 * 3_600 - 1_500 };
    assert!(!s12_holds(tb, late, EVM, 900, 600));
    // Earliest refund of B is T - 15; window = (T - 15) - (T - 1_500) - 900.
    assert_eq!(reveal_window(tb, late, EVM, 900), 1_500 - 15 - 900);
}

#[test]
fn arithmetic_at_the_limits() {
    let now = ChainNow { tip_height: 0, now_real: u64::MAX };
    let wide = ClockBounds { min_block_secs: u64::MAX, max_block_secs: u64::MAX, max_lead_secs: 0, max_lag_secs: u64::MAX };
    let t = Timelock::Height(u64::MAX);
    assert!(latest_exec(t, now, wide) > u64::MAX as u128);
    assert_eq!(timeout_gap(t, now, wide, Timelock::Height(1), ChainNow { tip_height: 0, now_real: 0 }, EVM), u64::MAX);
}

#[test]
fn rule_serde_shape() {
    let rule = SwapRule::All(vec![SwapRule::PeriodNotionalAtMost(86_400, 5), SwapRule::ContractPinned]);
    let json = serde_json::to_string(&rule).unwrap();
    assert_eq!(json, r#"{"all":[{"period_notional_at_most":[86400,5]},"contract_pinned"]}"#);
    assert_eq!(serde_json::from_str::<SwapRule>(&json).unwrap(), rule);
}
