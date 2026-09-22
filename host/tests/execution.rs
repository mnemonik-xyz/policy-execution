#[path = "../../core/examples/common/mod.rs"]
mod common;

use risc0_zkvm::{default_executor, ExecutorEnv};
use warrant_methods::WARRANT_GUEST_ELF;
use warrant_policy::{authorize, Input, Rule};

fn execute(input: &Input) -> anyhow::Result<Vec<u8>> {
    let env = ExecutorEnv::builder()
        .segment_limit_po2(18)
        .write(input)?
        .build()?;
    Ok(default_executor()
        .execute(env, WARRANT_GUEST_ELF)?
        .journal
        .bytes)
}

#[test]
fn accelerated_guest_matches_native_evaluation_for_different_policies() {
    let mut input = common::fixture();
    assert_eq!(
        execute(&input).unwrap(),
        authorize(&input).unwrap().journal()
    );
    input.policy.rule = Rule::Any(vec![Rule::AmountAtMost(1), Rule::Accepted]);
    input.request.recipient = [42; 20];
    input.evidence.vendor.recipient = input.request.recipient;
    input.evidence.acceptance.recipient = input.request.recipient;
    common::resign(&mut input);
    assert_eq!(
        execute(&input).unwrap(),
        authorize(&input).unwrap().journal()
    );
}

#[test]
fn accelerated_guest_rejects_forged_registry_and_reviewer_signatures() {
    for vendor in [true, false] {
        let mut input = common::fixture();
        if vendor {
            input.evidence.vendor_signature[0] ^= 1;
        } else {
            input.evidence.acceptance_signature[0] ^= 1;
        }
        assert!(authorize(&input).is_err());
        assert!(execute(&input).is_err());
    }
}

#[test]
fn guest_cannot_emit_authorization_for_denied_or_mismatched_task() {
    let mut denied = common::fixture();
    denied.evidence.acceptance.accepted = false;
    common::resign(&mut denied);
    assert!(execute(&denied).is_err());
    let mut mismatch = common::fixture();
    mismatch.request.deliverable_hash[0] ^= 1;
    assert!(execute(&mismatch).is_err());
}

#[test]
fn guest_enforces_verified_byte_equality_on_authenticated_facts() {
    let mut input = common::fixture();
    input.policy.rule = Rule::All(vec![
        Rule::RecipientEquals(input.request.recipient),
        Rule::DeliverableEquals(input.request.deliverable_hash),
    ]);
    assert_eq!(
        execute(&input).unwrap(),
        authorize(&input).unwrap().journal()
    );
    let original = input.clone();
    for recipient in [true, false] {
        input = original.clone();
        if recipient {
            input.request.recipient[19] ^= 1;
            input.evidence.vendor.recipient = input.request.recipient;
            input.evidence.acceptance.recipient = input.request.recipient;
        } else {
            input.request.deliverable_hash[31] ^= 1;
            input.evidence.acceptance.deliverable_hash = input.request.deliverable_hash;
        }
        common::resign(&mut input);
        assert!(execute(&input).is_err());
    }
}
