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
}

/// The caller supplies these facts only after authenticating and binding evidence.
pub struct Facts {
    pub amount: u64,
    pub category: u16,
    pub accepted: bool,
    pub deliverable: [u8; 32],
    pub recipient: [u8; 20],
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

} // verus!
