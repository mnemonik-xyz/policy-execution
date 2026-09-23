//! Synthetic invoice input for the local invoice-escrow demo. Writes `input.json`
//! (for `warrant-host invoice-prove`) and `terms.json` (for `InvoiceEscrow.offer`).
//! Never use these public signing keys for real payments.
#[path = "common/invoice.rs"]
mod invoice;
use invoice::*;
use std::{env, fs, path::Path};
use warrant_policy::evidence::*;
use warrant_policy::Scope;

fn bytes<const N: usize>(s: &str) -> [u8; N] {
    let s = s.strip_prefix("0x").unwrap_or(s);
    assert_eq!(s.len(), N * 2);
    std::array::from_fn(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap())
}

fn hex(b: &[u8]) -> String {
    format!(
        "0x{}",
        b.iter().map(|x| format!("{x:02x}")).collect::<String>()
    )
}

fn main() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(
        args.len(),
        7,
        "invoice_fixture out_dir chain_id escrow token vendor base_timestamp"
    );
    let scope = Scope {
        chain_id: args[2].parse().unwrap(),
        vault: bytes(&args[3]),
        token: bytes(&args[4]),
    };
    let mut input = fixture_at(doc().xml(), scope, args[6].parse().unwrap());
    input.vendor.recipient = bytes(&args[5]);
    resign(&mut input);
    let InvoiceOutcome::Allow(auth) = authorize_invoice(&input).unwrap() else {
        panic!("fixture must be allowed");
    };
    let out = Path::new(&args[1]);
    fs::write(
        out.join("input.json"),
        serde_json::to_vec_pretty(&input).unwrap(),
    )
    .unwrap();
    let terms = serde_json::json!({
        "policyHash": hex(&auth.authorization.policy_hash),
        "policyVersion": auth.authorization.policy_version,
        "poId": hex(&auth.po_id),
        "recipient": hex(&input.vendor.recipient),
        "maxTotal": auth.po_max_total,
        "amount": auth.authorization.request.amount,
        "taskId": hex(&auth.authorization.request.task_id),
    });
    fs::write(
        out.join("terms.json"),
        serde_json::to_vec_pretty(&terms).unwrap(),
    )
    .unwrap();
    println!("{}", serde_json::to_string_pretty(&terms).unwrap());
}
