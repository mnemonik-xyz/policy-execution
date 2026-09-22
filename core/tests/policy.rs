#[path = "../examples/common/mod.rs"]
mod common;

use warrant_policy::*;

#[test]
fn accepts_authenticated_task_and_intersects_time_windows() {
    let input = common::fixture();
    let auth = authorize(&input).unwrap();
    assert_eq!(auth.request, input.request);
    assert_eq!(auth.valid_after, 1200);
    assert_eq!(auth.valid_until, 4800);
}

#[test]
fn registered_recipient_can_change_without_changing_owner_policy() {
    let mut input = common::fixture();
    let original_policy = policy_hash(&input.policy);
    input.request.recipient = [42; 20];
    input.evidence.vendor.recipient = input.request.recipient;
    input.evidence.acceptance.recipient = input.request.recipient;
    common::resign(&mut input);
    let auth = authorize(&input).unwrap();
    assert_eq!(auth.policy_hash, original_policy);
    assert_eq!(auth.request.recipient, [42; 20]);
}

#[test]
fn changed_recipient_without_matching_credentials_is_denied() {
    let mut input = common::fixture();
    input.request.recipient = [42; 20];
    assert_eq!(authorize(&input), Err(Denial::RequestMismatch));
}

#[test]
fn self_reported_completion_cannot_replace_reviewer_evidence() {
    let mut input = common::fixture();
    input.evidence.acceptance.accepted = false;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));
    input.evidence.acceptance.accepted = true;
    assert_eq!(authorize(&input), Err(Denial::InvalidSignature));
}

#[test]
fn wrong_authority_and_corrupted_signatures_fail_closed() {
    for vendor in [true, false] {
        let mut input = common::fixture();
        if vendor {
            input.evidence.vendor_signature[0] ^= 1;
        } else {
            input.evidence.acceptance_signature[0] ^= 1;
        }
        assert_eq!(authorize(&input), Err(Denial::InvalidSignature));
    }
    let mut input = common::fixture();
    input.policy.acceptance_key = input.policy.registry_key.clone();
    assert_eq!(authorize(&input), Err(Denial::InvalidSignature));
}

#[test]
fn task_id_deliverable_and_amount_are_bound_to_acceptance() {
    for field in 0..3 {
        let mut input = common::fixture();
        match field {
            0 => input.request.task_id[0] ^= 1,
            1 => input.request.deliverable_hash[0] ^= 1,
            _ => input.request.amount -= 1,
        }
        assert_eq!(authorize(&input), Err(Denial::RequestMismatch));
    }
}

#[test]
fn signed_over_limit_request_is_still_denied() {
    let mut input = common::fixture();
    input.request.amount += 1;
    input.evidence.acceptance.amount = input.request.amount;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));
}

#[test]
fn exact_recipient_and_deliverable_rules_use_authenticated_request_fields() {
    let mut input = common::fixture();
    input.policy.rule = Rule::All(vec![
        Rule::RecipientEquals(input.request.recipient),
        Rule::DeliverableEquals(input.request.deliverable_hash),
        Rule::Accepted,
    ]);
    assert!(authorize(&input).is_ok());
    let original = input.clone();
    // The new recipient has valid matching signatures, but fails the policy.
    input.request.recipient[19] ^= 1;
    input.evidence.vendor.recipient = input.request.recipient;
    input.evidence.acceptance.recipient = input.request.recipient;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));

    input = original;
    input.request.deliverable_hash[31] ^= 1;
    input.evidence.acceptance.deliverable_hash = input.request.deliverable_hash;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));
}

#[test]
fn authenticated_category_cannot_be_substituted_by_the_agent() {
    let mut input = common::fixture();
    input.evidence.vendor.category = 8;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));
    input.evidence.vendor.category = 7;
    assert_eq!(authorize(&input), Err(Denial::InvalidSignature));
}

#[test]
fn all_and_any_support_new_policy_compositions_as_data() {
    let mut input = common::fixture();
    input.policy.rule = Rule::All(vec![
        Rule::Accepted,
        Rule::Any(vec![
            Rule::AmountAtMost(10),
            Rule::All(vec![
                Rule::AmountAtMost(100_000_000),
                Rule::VendorCategoryIn(vec![7]),
            ]),
        ]),
    ]);
    assert!(authorize(&input).is_ok());
    input.policy.rule = Rule::All(vec![Rule::Accepted, Rule::AmountAtMost(10)]);
    assert_eq!(authorize(&input), Err(Denial::PolicyDenied));
}

#[test]
fn changed_policy_changes_commitment_even_if_both_allow() {
    let mut input = common::fixture();
    let first = authorize(&input).unwrap();
    input.policy.rule = Rule::AmountAtMost(200_000_000);
    let second = authorize(&input).unwrap();
    assert_ne!(first.policy_hash, second.policy_hash);
    input.policy.version += 1;
    assert_ne!(second.policy_hash, policy_hash(&input.policy));
}

#[test]
fn scope_prevents_cross_chain_vault_and_token_evidence_reuse() {
    for field in 0..3 {
        let mut input = common::fixture();
        match field {
            0 => input.request.scope.chain_id += 1,
            1 => input.evidence.vendor.scope.vault[0] ^= 1,
            _ => input.evidence.acceptance.scope.token[0] ^= 1,
        }
        assert_eq!(authorize(&input), Err(Denial::ScopeMismatch));
    }
}

#[test]
fn incompatible_evidence_validity_intervals_are_rejected() {
    let mut input = common::fixture();
    input.evidence.acceptance.valid_after = 4901;
    input.evidence.acceptance.valid_until = 4999;
    common::resign(&mut input);
    assert_eq!(authorize(&input), Err(Denial::EmptyValidityWindow));
}

#[test]
fn empty_oversized_deep_and_unknown_policies_are_rejected() {
    for rule in [
        Rule::All(vec![]),
        Rule::Any(vec![]),
        Rule::VendorCategoryIn(vec![]),
        Rule::VendorCategoryIn(vec![0]),
        Rule::AmountAtMost(0),
        Rule::All(vec![Rule::Accepted; 17]),
    ] {
        let mut input = common::fixture();
        input.policy.rule = rule;
        assert_eq!(authorize(&input), Err(Denial::InvalidPolicy));
    }
    let mut input = common::fixture();
    for _ in 0..10 {
        input.policy.rule = Rule::All(vec![input.policy.rule]);
    }
    assert_eq!(authorize(&input), Err(Denial::InvalidPolicy));
    assert!(serde_json::from_str::<Rule>(r#"{"trust_agent":true}"#).is_err());
}

#[test]
fn policy_encoding_roundtrips_without_json_format_dependence() {
    let input = common::fixture();
    let compact = serde_json::to_string(&input).unwrap();
    let pretty = serde_json::to_string_pretty(&input).unwrap();
    let a: Input = serde_json::from_str(&compact).unwrap();
    let b: Input = serde_json::from_str(&pretty).unwrap();
    assert_eq!(authorize(&a), authorize(&b));
    let binary = bincode::serialize(&input).unwrap();
    let roundtrip: Input = bincode::deserialize(&binary).unwrap();
    assert_eq!(authorize(&input), authorize(&roundtrip));
}

#[test]
fn uint64_maximum_does_not_wrap_and_zero_payment_is_rejected() {
    let mut input = common::fixture();
    input.policy.rule = Rule::AmountAtMost(u64::MAX);
    input.request.amount = u64::MAX;
    input.evidence.acceptance.amount = u64::MAX;
    common::resign(&mut input);
    assert!(authorize(&input).is_ok());
    input.request.amount = 0;
    assert_eq!(authorize(&input), Err(Denial::InvalidRequest));
}

#[test]
fn abi_journal_has_fixed_length_and_binds_all_payment_fields() {
    let auth = authorize(&common::fixture()).unwrap();
    let bytes = auth.journal();
    assert_eq!(bytes.len(), 12 * 32);
    assert_eq!(&bytes[0..32], &auth.policy_hash);
    assert_eq!(&bytes[4 * 32 + 12..5 * 32], &auth.request.recipient);
    assert_eq!(
        &bytes[5 * 32 + 24..6 * 32],
        &auth.request.amount.to_be_bytes()
    );
    assert_eq!(&bytes[6 * 32..7 * 32], &auth.request.task_id);
    assert_eq!(&bytes[7 * 32..8 * 32], &auth.request.deliverable_hash);
    assert_eq!(&bytes[11 * 32..12 * 32], &auth.evidence_hash);
}
