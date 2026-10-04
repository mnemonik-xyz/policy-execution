#[path = "common/solver.rs"]
mod common;
use warrant_policy::solver::{authorize_solver, result_bytes};

fn hex(bytes: &[u8]) -> String {
    let mut output = String::from("0x");
    for byte in bytes {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

fn main() {
    let input = common::fixture();
    if std::env::args().nth(1).as_deref() == Some("journal") {
        let auth = authorize_solver(&input).unwrap();
        println!("{}",serde_json::to_string_pretty(&serde_json::json!({
            "comment":"Synthetic solver fixture, not a proof.",
            "journal":hex(&auth.journal()),"result":hex(&result_bytes(&input.schedule).unwrap()),
            "policyHash":hex(&auth.authorization.policy_hash),"totalCost":auth.total_cost
        })).unwrap());
    } else {
        println!("{}", serde_json::to_string_pretty(&input).unwrap());
    }
}
