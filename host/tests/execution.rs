#[path = "../../core/examples/common/mod.rs"]
mod common;

#[path = "../../core/examples/common/invoice.rs"]
mod invoice_fixture;

use risc0_zkvm::{default_executor, ExecutorEnv};
use warrant_methods::WARRANT_GUEST_ELF;
use warrant_policy::{authorize, Input, Rule};

#[test]
fn invoice_guest_rejects_tampered_and_unauthenticated_documents() {
    use warrant_methods::WARRANT_INVOICE_GUEST_ELF;
    let original = invoice_fixture::fixture_at(
        invoice_fixture::doc().xml(),
        warrant_policy::Scope {
            chain_id: 31337,
            vault: [3; 20],
            token: [4; 20],
        },
        1000,
    );
    for missing in [false, true] {
        let mut input = original.clone();
        if missing {
            input.invoice_attestation = None;
            input.invoice_signature = None;
        } else {
            let mut document = invoice_fixture::doc();
            document.number = "TAMPERED";
            input.document = document.xml();
        }
        let env = ExecutorEnv::builder()
            .segment_limit_po2(18)
            .write(&input)
            .unwrap()
            .build()
            .unwrap();
        assert!(default_executor()
            .execute(env, WARRANT_INVOICE_GUEST_ELF)
            .is_err());
    }
}

#[test]
fn invoice_guest_matches_native_customer_bound_journal() {
    use warrant_methods::WARRANT_INVOICE_GUEST_ELF;
    use warrant_policy::evidence::{authorize_invoice, InvoiceOutcome};
    let mut input = invoice_fixture::fixture_at(
        invoice_fixture::doc().xml(),
        warrant_policy::Scope {
            chain_id: 31337,
            vault: [3; 20],
            token: [4; 20],
        },
        1000,
    );
    for customer in [[8; 20], [9; 20]] {
        input.policy.customer = customer;
        invoice_fixture::attest(&mut input);
        let native = match authorize_invoice(&input).unwrap() {
            InvoiceOutcome::Allow(auth) => auth.journal(),
            other => panic!("unexpected outcome: {other:?}"),
        };
        let env = ExecutorEnv::builder()
            .segment_limit_po2(18)
            .write(&input)
            .unwrap()
            .build()
            .unwrap();
        let session = default_executor()
            .execute(env, WARRANT_INVOICE_GUEST_ELF)
            .unwrap();
        assert_eq!(session.journal.bytes, native);
        assert_eq!(native.len(), 480);
        assert_eq!(&native[460..], &customer);
    }
}

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
