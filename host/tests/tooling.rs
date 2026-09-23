#[path = "../../core/examples/common/mod.rs"]
mod common;
use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};
use warrant_policy::authorize;
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
