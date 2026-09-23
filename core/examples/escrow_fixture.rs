//! Synthetic evidence for the local escrow demo. Never use these public signing keys for real tasks.
mod common;
use std::{env, fs};
use warrant_policy::{authorize, Policy};
fn bytes<const N: usize>(s: &str) -> [u8; N] {
    let s = s.strip_prefix("0x").unwrap_or(s);
    assert_eq!(s.len(), N * 2);
    std::array::from_fn(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap())
}
fn main() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(
        args.len(),
        5,
        "escrow_fixture policy.json taskId recipient amount"
    );
    let policy: Policy = serde_json::from_slice(&fs::read(&args[1]).unwrap()).unwrap();
    let mut input = common::fixture();
    input.policy = policy;
    input.request.scope = input.policy.scope.clone();
    input.request.task_id = bytes(&args[2]);
    input.request.recipient = bytes(&args[3]);
    input.request.amount = args[4].parse().unwrap();
    input.evidence.vendor.scope = input.request.scope.clone();
    input.evidence.vendor.recipient = input.request.recipient;
    input.evidence.vendor.valid_after = input.policy.valid_after;
    input.evidence.vendor.valid_until = input.policy.valid_until;
    input.evidence.acceptance.scope = input.request.scope.clone();
    input.evidence.acceptance.task_id = input.request.task_id;
    input.evidence.acceptance.recipient = input.request.recipient;
    input.evidence.acceptance.amount = input.request.amount;
    input.evidence.acceptance.valid_after = input.policy.valid_after;
    input.evidence.acceptance.valid_until = input.policy.valid_until;
    common::resign(&mut input);
    authorize(&input).unwrap();
    println!("{}", serde_json::to_string_pretty(&input).unwrap());
}
