//! Executable policy evaluation and its mathematical specification share this source.
//! Authentication, parsing, policy validation and payment settlement are outside this proof.
use vstd::prelude::*;

verus! {

#[cfg_attr(feature = "serde", derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize))]
#[cfg_attr(feature = "serde", serde(rename_all = "snake_case"))]
pub enum Rule {
    All(Vec<Rule>),
    Any(Vec<Rule>),
    AmountAtMost(u64),
    VendorCategoryIn(Vec<u16>),
    Accepted,
    DeliverableEquals([u8; 32]),
    RecipientEquals([u8; 20]),
    /// Every invoice line has an admitted label in the set; no lines never satisfies.
    LineLabelsWithin(Vec<u16>),
    /// The deterministic scan found no owner-denied term on any invoice line.
    NoDeniedTerm,
    /// The payment fits in what remains of its purchase order.
    WithinPo,
}

/// The caller supplies these facts only after authenticating and binding evidence.
pub struct Facts {
    pub amount: u64,
    pub category: u16,
    pub accepted: bool,
    pub deliverable: [u8; 32],
    pub recipient: [u8; 20],
    pub line_labels: Vec<u16>,
    pub no_denied_term: bool,
    pub po_remaining: u64,
}

/// Declarative policy meaning: conjunction, disjunction, membership and equality.
/// Empty All is true and empty Any false; the outer policy validator rejects both.
pub open spec fn satisfies(rule: &Rule, facts: &Facts) -> bool
    decreases rule,
{
    match rule {
        Rule::All(children) => forall|i: int| 0 <= i < children.len()
            ==> satisfies(#[trigger] &children[i], facts),
        Rule::Any(children) => exists|i: int| 0 <= i < children.len()
            && satisfies(#[trigger] &children[i], facts),
        Rule::AmountAtMost(cap) => facts.amount <= *cap,
        Rule::VendorCategoryIn(categories) => categories@.contains(facts.category),
        Rule::Accepted => facts.accepted,
        Rule::DeliverableEquals(hash) => facts.deliverable@ == hash@,
        Rule::RecipientEquals(recipient) => facts.recipient@ == recipient@,
        Rule::LineLabelsWithin(labels) => facts.line_labels.len() > 0
            && forall|i: int| 0 <= i < facts.line_labels.len()
                ==> labels@.contains(#[trigger] facts.line_labels[i]),
        Rule::NoDeniedTerm => facts.no_denied_term,
        Rule::WithinPo => facts.amount <= facts.po_remaining,
    }
}

fn bytes_equal<const N: usize>(a: &[u8; N], b: &[u8; N]) -> (result: bool)
    ensures result == (a@ == b@),
{
    let mut i: usize = 0;
    while i < N
        invariant
            i <= N,
            forall|j: int| 0 <= j < i ==> a[j] == b[j],
        decreases N - i,
    {
        if a[i] != b[i] {
            return false;
        }
        i += 1;
    }
    assert(a@ =~= b@);
    true
}

fn labels_within(labels: &Vec<u16>, lines: &Vec<u16>) -> (result: bool)
    ensures
        result == (lines.len() > 0 && forall|i: int| 0 <= i < lines.len()
            ==> labels@.contains(#[trigger] lines[i])),
{
    if lines.len() == 0 {
        return false;
    }
    let mut i: usize = 0;
    while i < lines.len()
        invariant
            i <= lines.len(),
            forall|j: int| 0 <= j < i ==> labels@.contains(#[trigger] lines[j]),
        decreases lines.len() - i,
    {
        if !category_contains(labels, lines[i]) {
            return false;
        }
        i += 1;
    }
    true
}

fn category_contains(categories: &Vec<u16>, category: u16) -> (result: bool)
    ensures result == categories@.contains(category),
{
    let mut i: usize = 0;
    while i < categories.len()
        invariant
            i <= categories.len(),
            forall|j: int| 0 <= j < i ==> categories[j] != category,
        decreases categories.len() - i,
    {
        if categories[i] == category {
            return true;
        }
        i += 1;
    }
    false
}

/// Soundness and completeness: the executable result equals declarative meaning.
#[verifier::loop_isolation(false)]
pub fn evaluate(rule: &Rule, facts: &Facts) -> (result: bool)
    ensures result == satisfies(rule, facts),
    decreases rule,
{
    match rule {
        Rule::All(children) => {
            let mut i: usize = 0;
            while i < children.len()
                invariant
                    i <= children.len(),
                    forall|j: int| 0 <= j < i ==> satisfies(#[trigger] &children[j], facts),
                decreases children.len() - i,
            {
                if !evaluate(&children[i], facts) {
                    return false;
                }
                i += 1;
            }
            true
        },
        Rule::Any(children) => {
            let mut i: usize = 0;
            while i < children.len()
                invariant
                    i <= children.len(),
                    forall|j: int| 0 <= j < i ==> !satisfies(#[trigger] &children[j], facts),
                decreases children.len() - i,
            {
                if evaluate(&children[i], facts) {
                    return true;
                }
                i += 1;
            }
            false
        },
        Rule::AmountAtMost(cap) => facts.amount <= *cap,
        Rule::VendorCategoryIn(categories) => category_contains(categories, facts.category),
        Rule::Accepted => facts.accepted,
        Rule::DeliverableEquals(hash) => bytes_equal(&facts.deliverable, hash),
        Rule::RecipientEquals(recipient) => bytes_equal(&facts.recipient, recipient),
        Rule::LineLabelsWithin(labels) => labels_within(labels, &facts.line_labels),
        Rule::NoDeniedTerm => facts.no_denied_term,
        Rule::WithinPo => facts.amount <= facts.po_remaining,
    }
}

/// Example policy: acceptance AND amount limit AND approved vendor category.
/// This theorem spells out its business-level consequences for every Facts value.
pub proof fn contractor_policy_guarantees(
    rule: &Rule, facts: &Facts, cap: u64, categories: Vec<u16>,
)
    requires
        match rule {
            Rule::All(children) => children.len() == 3
                && children[0] == Rule::Accepted
                && children[1] == Rule::AmountAtMost(cap)
                && children[2] == Rule::VendorCategoryIn(categories),
            _ => false,
        },
        satisfies(rule, facts),
    ensures
        facts.accepted,
        facts.amount <= cap,
        categories@.contains(facts.category),
{
    match rule {
        Rule::All(children) => {
            assert(satisfies(&children[0], facts));
            assert(satisfies(&children[1], facts));
            assert(satisfies(&children[2], facts));
        },
        _ => {},
    }
}


/// Facts whose Tier 1 and optional parts may be unknown. Signed and derived fields
/// that select or quantify the payment are always known before evaluation.
pub struct Facts3 {
    pub amount: u64,
    pub category: u16,
    pub accepted: Option<bool>,
    pub deliverable: [u8; 32],
    pub recipient: [u8; 20],
    pub line_labels: Vec<Option<u16>>,
    pub no_denied_term: Option<bool>,
    pub po_remaining: Option<u64>,
}

#[cfg_attr(feature = "serde", derive(Clone, Copy, Debug, PartialEq, Eq))]
pub enum Decision {
    Allow,
    Deny,
    Ask,
}

pub open spec fn agrees<T>(known: Option<T>, value: T) -> bool {
    match known {
        Some(v) => v == value,
        None => true,
    }
}

/// `facts` is one way of filling in every unknown in `f3`.
pub open spec fn completes(f3: &Facts3, facts: &Facts) -> bool {
    &&& facts.amount == f3.amount
    &&& facts.category == f3.category
    &&& agrees(f3.accepted, facts.accepted)
    &&& facts.deliverable@ == f3.deliverable@
    &&& facts.recipient@ == f3.recipient@
    &&& facts.line_labels.len() == f3.line_labels.len()
    &&& forall|i: int| 0 <= i < f3.line_labels.len()
        ==> agrees(#[trigger] f3.line_labels[i], facts.line_labels[i])
    &&& agrees(f3.no_denied_term, facts.no_denied_term)
    &&& agrees(f3.po_remaining, facts.po_remaining)
}

/// Strong Kleene semantics: `Some(b)` only when every completion evaluates to `b`.
pub open spec fn kleene(rule: &Rule, f3: &Facts3) -> Option<bool>
    decreases rule,
{
    match rule {
        Rule::All(children) => {
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
        Rule::Any(children) => {
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
        Rule::AmountAtMost(cap) => Some(f3.amount <= *cap),
        Rule::VendorCategoryIn(categories) => Some(categories@.contains(f3.category)),
        Rule::Accepted => f3.accepted,
        Rule::DeliverableEquals(hash) => Some(f3.deliverable@ == hash@),
        Rule::RecipientEquals(recipient) => Some(f3.recipient@ == recipient@),
        Rule::LineLabelsWithin(labels) => {
            if f3.line_labels.len() == 0 {
                Some(false)
            } else if exists|i: int| 0 <= i < f3.line_labels.len()
                && label_outside(labels, #[trigger] f3.line_labels[i]) {
                Some(false)
            } else if forall|i: int| 0 <= i < f3.line_labels.len()
                ==> (#[trigger] f3.line_labels[i]) is Some {
                Some(true)
            } else {
                None
            }
        },
        Rule::NoDeniedTerm => f3.no_denied_term,
        Rule::WithinPo => match f3.po_remaining {
            Some(remaining) => Some(f3.amount <= remaining),
            None => None,
        },
    }
}

pub open spec fn label_outside(labels: &Vec<u16>, line: Option<u16>) -> bool {
    match line {
        Some(label) => !labels@.contains(label),
        None => false,
    }
}

/// Soundness: a known three-valued result holds for every completion of the facts.
pub proof fn kleene_sound(rule: &Rule, f3: &Facts3, facts: &Facts)
    requires completes(f3, facts),
    ensures
        kleene(rule, f3) == Some(true) ==> satisfies(rule, facts),
        kleene(rule, f3) == Some(false) ==> !satisfies(rule, facts),
    decreases rule,
{
    match rule {
        Rule::All(children) => {
            assert forall|i: int| 0 <= i < children.len() implies
                (kleene(#[trigger] &children[i], f3) == Some(true) ==> satisfies(&children[i], facts))
                && (kleene(&children[i], f3) == Some(false) ==> !satisfies(&children[i], facts)) by {
                kleene_sound(&children[i], f3, facts);
            }
        },
        Rule::Any(children) => {
            assert forall|i: int| 0 <= i < children.len() implies
                (kleene(#[trigger] &children[i], f3) == Some(true) ==> satisfies(&children[i], facts))
                && (kleene(&children[i], f3) == Some(false) ==> !satisfies(&children[i], facts)) by {
                kleene_sound(&children[i], f3, facts);
            }
        },
        Rule::LineLabelsWithin(labels) => {
            if kleene(rule, f3) == Some(false) && f3.line_labels.len() > 0 {
                let i = choose|i: int| 0 <= i < f3.line_labels.len()
                    && label_outside(labels, #[trigger] f3.line_labels[i]);
                assert(agrees(f3.line_labels[i], facts.line_labels[i]));
            }
            if kleene(rule, f3) == Some(true) {
                assert forall|i: int| 0 <= i < facts.line_labels.len() implies
                    labels@.contains(#[trigger] facts.line_labels[i]) by {
                    assert(f3.line_labels[i] is Some);
                    assert(!label_outside(labels, f3.line_labels[i]));
                    assert(agrees(f3.line_labels[i], facts.line_labels[i]));
                }
            }
        },
        _ => {},
    }
}

/// Allow and Deny are each correct for every way of filling in the unknowns.
pub proof fn decision_sound(rule: &Rule, f3: &Facts3)
    ensures
        kleene(rule, f3) == Some(true) ==> forall|facts: Facts|
            completes(f3, &facts) ==> #[trigger] satisfies(rule, &facts),
        kleene(rule, f3) == Some(false) ==> forall|facts: Facts|
            completes(f3, &facts) ==> !#[trigger] satisfies(rule, &facts),
{
    assert forall|facts: Facts| completes(f3, &facts) implies
        (kleene(rule, f3) == Some(true) ==> #[trigger] satisfies(rule, &facts))
        && (kleene(rule, f3) == Some(false) ==> !satisfies(rule, &facts)) by {
        kleene_sound(rule, f3, &facts);
    }
}

fn labels_within3(labels: &Vec<u16>, lines: &Vec<Option<u16>>) -> (result: Option<bool>)
    ensures
        result == (if lines.len() == 0 {
            Some(false)
        } else if exists|i: int| 0 <= i < lines.len() && label_outside(labels, #[trigger] lines[i]) {
            Some(false)
        } else if forall|i: int| 0 <= i < lines.len() ==> (#[trigger] lines[i]) is Some {
            Some(true)
        } else {
            None
        }),
{
    if lines.len() == 0 {
        return Some(false);
    }
    let mut unknown = false;
    let mut i: usize = 0;
    while i < lines.len()
        invariant
            i <= lines.len(),
            forall|j: int| 0 <= j < i ==> !label_outside(labels, #[trigger] lines[j]),
            unknown == exists|j: int| 0 <= j < i && (#[trigger] lines[j]) is None,
        decreases lines.len() - i,
    {
        match lines[i] {
            Some(label) => {
                if !category_contains(labels, label) {
                    assert(label_outside(labels, lines[i as int]));
                    return Some(false);
                }
            },
            None => {
                unknown = true;
            },
        }
        i += 1;
    }
    if unknown {
        None
    } else {
        assert forall|j: int| 0 <= j < lines.len() implies (#[trigger] lines[j]) is Some by {
            if lines[j] is None {
                assert(exists|k: int| 0 <= k < i && (#[trigger] lines[k]) is None);
            }
        }
        Some(true)
    }
}

/// Executable strong Kleene evaluation; equal to `kleene` for every rule and facts.
#[verifier::loop_isolation(false)]
pub fn evaluate3(rule: &Rule, f3: &Facts3) -> (result: Option<bool>)
    ensures result == kleene(rule, f3),
    decreases rule,
{
    match rule {
        Rule::All(children) => {
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
        Rule::Any(children) => {
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
        Rule::AmountAtMost(cap) => Some(f3.amount <= *cap),
        Rule::VendorCategoryIn(categories) => Some(category_contains(categories, f3.category)),
        Rule::Accepted => f3.accepted,
        Rule::DeliverableEquals(hash) => Some(bytes_equal(&f3.deliverable, hash)),
        Rule::RecipientEquals(recipient) => Some(bytes_equal(&f3.recipient, recipient)),
        Rule::LineLabelsWithin(labels) => labels_within3(labels, &f3.line_labels),
        Rule::NoDeniedTerm => f3.no_denied_term,
        Rule::WithinPo => match f3.po_remaining {
            Some(remaining) => Some(f3.amount <= remaining),
            None => None,
        },
    }
}

/// Allow only when the policy holds for every completion; Deny only when it fails
/// for every completion; otherwise Ask. Only Allow may authorize a payment.
pub fn decide(rule: &Rule, f3: &Facts3) -> (result: Decision)
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

/// Final invoice payment fields constructed from the very facts that were evaluated.
/// Authentication of those facts remains the evidence adapter's responsibility.
pub struct PolicyPayment {
    pub recipient: [u8; 20],
    pub document: [u8; 32],
    pub amount: u64,
    pub valid_after: u64,
    pub valid_until: u64,
}

/// Runtime constructor: no caller-supplied recipient/amount can be substituted
/// after the policy decision. Empty windows and zero payments never authorize.
pub fn authorize_payment(rule: &Rule, facts: &Facts3, after: u64, until: u64)
    -> (result: Option<PolicyPayment>)
    ensures
        result is Some ==> {
            let p = result->Some_0;
            &&& p.amount == facts.amount
            &&& p.amount > 0
            &&& p.recipient@ == facts.recipient@
            &&& p.document@ == facts.deliverable@
            &&& p.valid_after == after
            &&& p.valid_until == until
            &&& after <= until
            &&& kleene(rule, facts) == Some(true)
            &&& forall|complete: Facts| completes(facts, &complete)
                ==> #[trigger] satisfies(rule, &complete)
        },
        result is Some <==> (facts.amount > 0 && after <= until && kleene(rule, facts) == Some(true)),
{
    if facts.amount == 0 || after > until {
        return None;
    }
    match decide(rule, facts) {
        Decision::Allow => {
            proof { decision_sound(rule, facts); }
            Some(PolicyPayment {
                recipient: facts.recipient,
                document: facts.deliverable,
                amount: facts.amount,
                valid_after: after,
                valid_until: until,
            })
        },
        _ => None,
    }
}

/// Exact intersection of two signed validity windows.
pub open spec fn in_window(after: u64, until: u64, now: u64) -> bool {
    after <= now && now <= until
}

pub fn intersect_window(a0: u64, a1: u64, b0: u64, b1: u64) -> (r: (u64, u64))
    ensures
        r.0 == if a0 >= b0 { a0 } else { b0 },
        r.1 == if a1 <= b1 { a1 } else { b1 },
        forall|now: u64| #[trigger] in_window(r.0, r.1, now)
            <==> (a0 <= now && now <= a1 && b0 <= now && now <= b1),
{
    (if a0 >= b0 { a0 } else { b0 }, if a1 <= b1 { a1 } else { b1 })
}

/// Floor conversion from invoice minor units to token base units. Neither a zero
/// result nor a value exceeding the journal's u64 amount can authorize payment.
pub fn convert_amount(minor: u64, numerator: u64, denominator: u64) -> (r: Option<u64>)
    ensures
        r is Some ==> denominator > 0 && r->Some_0 > 0
            && r->Some_0 as int == (minor as int * numerator as int) / denominator as int,
        r is Some <==> (denominator > 0
            && 0 < (minor as int * numerator as int) / denominator as int
            && (minor as int * numerator as int) / denominator as int <= u64::MAX),
{
    if denominator == 0 { return None; }
    assert((minor as int) * (numerator as int) <= u128::MAX) by(nonlinear_arith)
        requires minor <= u64::MAX, numerator <= u64::MAX;
    let product = (minor as u128) * (numerator as u128);
    let amount = product / (denominator as u128);
    if amount == 0 || amount > u64::MAX as u128 { None }
    else { Some(amount as u64) }
}

} // verus!
