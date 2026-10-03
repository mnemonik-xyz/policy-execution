#[path = "../../core/examples/common/mod.rs"]
mod common;
#[path = "../../core/examples/common/invoice.rs"]
mod invoice_fixture;
use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
use warrant_policy::authorize;

#[test]
fn invoice_issuer_cli_signs_exact_bytes_and_refuses_overwrite() {
    use warrant_policy::evidence::{authorize_invoice, InvoiceOutcome};
    let dir = std::env::temp_dir().join(format!(
        "warrant-invoice-signing-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&dir).unwrap();
    let mut input = invoice_fixture::fixture_at(
        invoice_fixture::doc().xml(),
        warrant_policy::Scope {
            chain_id: 31337,
            vault: [3; 20],
            token: [4; 20],
        },
        1000,
    );
    let key = dir.join("issuer.key");
    let statement = dir.join("statement.json");
    let signature = dir.join("signature.json");
    fs::write(&key, "05".repeat(32)).unwrap();
    let policy = dir.join("policy.json");
    let po = dir.join("po.json");
    let document = dir.join("invoice.xml");
    fs::write(&policy, serde_json::to_vec(&input.policy).unwrap()).unwrap();
    fs::write(&po, serde_json::to_vec(&input.po).unwrap()).unwrap();
    fs::write(&document, &input.document).unwrap();
    let prepare = || {
        Command::new(env!("CARGO_BIN_EXE_warrant-evidence"))
            .arg("prepare-invoice")
            .arg(&policy)
            .arg(&po)
            .arg(&document)
            .arg(&statement)
            .output()
            .unwrap()
    };
    let result = prepare();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let prepared = fs::read(&statement).unwrap();
    assert!(!prepare().status.success());
    assert_eq!(prepared, fs::read(&statement).unwrap());
    input.invoice_attestation = Some(serde_json::from_slice(&prepared).unwrap());
    let run = || {
        Command::new(env!("CARGO_BIN_EXE_warrant-evidence"))
            .arg("sign-invoice")
            .arg(&key)
            .arg(&statement)
            .arg(&signature)
            .output()
            .unwrap()
    };
    let result = run();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let bytes = fs::read(&signature).unwrap();
    assert!(!run().status.success());
    assert_eq!(bytes, fs::read(&signature).unwrap());
    input.invoice_signature = Some(serde_json::from_slice(&bytes).unwrap());
    assert!(matches!(
        authorize_invoice(&input).unwrap(),
        InvoiceOutcome::Allow(_)
    ));
    let mut document = invoice_fixture::doc();
    document.number = "RENUMBERED";
    input.document = document.xml();
    assert!(authorize_invoice(&input).is_err());
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn issuer_cli_signatures_authorize_and_refuse_overwrite() {
    let dir = std::env::temp_dir().join(format!(
        "warrant-signing-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&dir).unwrap();
    let mut input = common::fixture();
    input.evidence.vendor.category = 9;
    input.evidence.acceptance.valid_until -= 1;
    for (kind, key, statement) in [
        (
            "vendor",
            "01".repeat(32),
            serde_json::to_vec(&input.evidence.vendor).unwrap(),
        ),
        (
            "acceptance",
            "02".repeat(32),
            serde_json::to_vec(&input.evidence.acceptance).unwrap(),
        ),
    ] {
        let key_path = dir.join(format!("{kind}.key"));
        let statement_path = dir.join(format!("{kind}.json"));
        let output_path = dir.join(format!("{kind}.sig.json"));
        fs::write(&key_path, key).unwrap();
        fs::write(&statement_path, statement).unwrap();
        let run = || {
            Command::new(env!("CARGO_BIN_EXE_warrant-evidence"))
                .arg(format!("sign-{kind}"))
                .arg(&key_path)
                .arg(&statement_path)
                .arg(&output_path)
                .output()
                .unwrap()
        };
        let result = run();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let bytes = fs::read(&output_path).unwrap();
        assert!(!run().status.success());
        assert_eq!(bytes, fs::read(&output_path).unwrap());
        let signature = serde_json::from_slice(&bytes).unwrap();
        if kind == "vendor" {
            input.evidence.vendor_signature = signature
        } else {
            input.evidence.acceptance_signature = signature
        }
    }
    assert!(authorize(&input).is_ok());
    input.evidence.acceptance.amount += 1;
    assert!(authorize(&input).is_err());
    fs::remove_dir_all(dir).unwrap();
}
