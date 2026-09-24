//! Synthetic invoice input for the local invoice-escrow demo. Writes `input.json`
//! (for `warrant-host invoice-prove`), `policy.json` and `request.json` (what the
//! signing service holds and what the agent sends it) and `terms.json` (for
//! `InvoiceEscrow.offer`). With a trailing `ask` argument the second line has no
//! order match and no claim, so the checker asks instead of allowing.
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
    assert!(
        (7..=9).contains(&args.len()),
        "invoice_fixture out_dir chain_id escrow token vendor base_timestamp [invoice_number] [ask]"
    );
    let ask = args.get(8).map(String::as_str) == Some("ask");
    let scope = Scope {
        chain_id: args[2].parse().unwrap(),
        vault: bytes(&args[3]),
        token: bytes(&args[4]),
    };
    let mut document = doc();
    if let Some(number) = args.get(7) {
        document.number = number.clone().leak();
    }
    if ask {
        // A small invoice with a line nobody can label: no seller item identifier,
        // no lexicon term.
        document.lines[0].amount = "100.00";
        document.lines[1] = Line {
            name: "Miscellaneous charges",
            item_id: None,
            amount: "200.00",
        };
        document.line_total = "300.00";
        document.tax = "30.00";
        document.inclusive = "330.00";
        document.payable = "330.00";
    }
    let mut input = fixture_at(document.xml(), scope, args[6].parse().unwrap());
    input.vendor.recipient = bytes(&args[5]);
    if ask {
        input.claims.truncate(1);
    }
    resign(&mut input);
    let facts = parse_invoice(&input.document).unwrap();
    let obligation = obligation_id(&facts.seller_tax_id, &facts.invoice_number);
    let out = Path::new(&args[1]);
    fs::create_dir_all(out).unwrap();
    fs::write(
        out.join("input.json"),
        serde_json::to_vec_pretty(&input).unwrap(),
    )
    .unwrap();
    fs::write(
        out.join("policy.json"),
        serde_json::to_vec_pretty(&input.policy).unwrap(),
    )
    .unwrap();
    let request = SignRequest::from(input.clone());
    fs::write(
        out.join("request.json"),
        serde_json::to_vec_pretty(&request).unwrap(),
    )
    .unwrap();
    let terms = match authorize_invoice(&input).unwrap() {
        InvoiceOutcome::Allow(auth) => {
            assert!(!ask, "ask fixture must not be allowed");
            serde_json::json!({
                "policyHash": hex(&auth.authorization.policy_hash),
                "policyVersion": auth.authorization.policy_version,
                "poId": hex(&auth.po_id),
                "recipient": hex(&input.vendor.recipient),
                "maxTotal": auth.po_max_total,
                "amount": auth.authorization.request.amount,
                "taskId": hex(&auth.authorization.request.task_id),
                "documentHash": hex(&facts.doc_hash),
            })
        }
        InvoiceOutcome::Ask(reasons) => {
            assert!(ask, "fixture must be allowed: {reasons:?}");
            serde_json::json!({
                "policyHash": hex(&invoice_policy_hash(&input.policy)),
                "policyVersion": input.policy.version,
                "poId": hex(&input.po.po_id),
                "recipient": hex(&input.vendor.recipient),
                "maxTotal": input.po.max_total,
                "amount": facts.payable,
                "taskId": hex(&obligation),
                "documentHash": hex(&facts.doc_hash),
                "ask": reasons.iter().map(|r| format!("{r:?}")).collect::<Vec<_>>(),
            })
        }
    };
    fs::write(
        out.join("terms.json"),
        serde_json::to_vec_pretty(&terms).unwrap(),
    )
    .unwrap();
    println!("{}", serde_json::to_string_pretty(&terms).unwrap());
}
