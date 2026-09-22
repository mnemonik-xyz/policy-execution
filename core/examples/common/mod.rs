use k256::ecdsa::{signature::Signer, Signature, SigningKey};
use warrant_policy::*;

// Public fixture keys authenticate synthetic test evidence only. No wallet use.
pub fn registry() -> SigningKey {
    SigningKey::from_slice(&[1; 32]).unwrap()
}
pub fn reviewer() -> SigningKey {
    SigningKey::from_slice(&[2; 32]).unwrap()
}

pub fn resign(input: &mut Input) {
    let vendor_sig: Signature = registry().sign(&vendor_message(&input.evidence.vendor));
    let acceptance_sig: Signature =
        reviewer().sign(&acceptance_message(&input.evidence.acceptance));
    input.evidence.vendor_signature = vendor_sig.to_bytes().to_vec();
    input.evidence.acceptance_signature = acceptance_sig.to_bytes().to_vec();
}

pub fn fixture() -> Input {
    let scope = Scope {
        chain_id: 31337,
        vault: [3; 20],
        token: [4; 20],
    };
    let request = Request {
        scope: scope.clone(),
        task_id: [5; 32],
        deliverable_hash: [6; 32],
        recipient: [7; 20],
        amount: 100_000_000,
    };
    let policy = Policy {
        version: 1,
        scope: scope.clone(),
        valid_after: 1000,
        valid_until: 5000,
        registry_key: registry()
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec(),
        acceptance_key: reviewer()
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec(),
        rule: Rule::All(vec![
            Rule::Accepted,
            Rule::AmountAtMost(100_000_000),
            Rule::VendorCategoryIn(vec![7, 9]),
        ]),
    };
    let evidence = Evidence {
        vendor: VendorCredential {
            scope: scope.clone(),
            recipient: request.recipient,
            category: 7,
            valid_after: 1100,
            valid_until: 4900,
        },
        vendor_signature: vec![],
        acceptance: Acceptance {
            scope,
            task_id: request.task_id,
            deliverable_hash: request.deliverable_hash,
            recipient: request.recipient,
            amount: request.amount,
            accepted: true,
            valid_after: 1200,
            valid_until: 4800,
        },
        acceptance_signature: vec![],
    };
    let mut input = Input {
        policy,
        request,
        evidence,
    };
    resign(&mut input);
    input
}
