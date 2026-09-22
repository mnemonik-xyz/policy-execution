mod common;

fn main() {
    let mut input = common::fixture();
    if std::env::args().nth(1).as_deref() == Some("alternative") {
        // Different recipient AND different rule composition; same interpreter.
        input.policy.version = 2;
        input.policy.rule = warrant_policy::Rule::All(vec![
            warrant_policy::Rule::Accepted,
            warrant_policy::Rule::Any(vec![
                warrant_policy::Rule::All(vec![
                    warrant_policy::Rule::VendorCategoryIn(vec![7]),
                    warrant_policy::Rule::AmountAtMost(100_000_000),
                ]),
                warrant_policy::Rule::All(vec![
                    warrant_policy::Rule::VendorCategoryIn(vec![9]),
                    warrant_policy::Rule::AmountAtMost(50_000_000),
                ]),
            ]),
        ]);
        input.request.recipient = [8; 20];
        input.request.amount = 50_000_000;
        input.request.task_id = [9; 32];
        input.evidence.vendor.recipient = input.request.recipient;
        input.evidence.vendor.category = 9;
        input.evidence.acceptance.recipient = input.request.recipient;
        input.evidence.acceptance.amount = input.request.amount;
        input.evidence.acceptance.task_id = input.request.task_id;
        common::resign(&mut input);
    }
    println!("{}", serde_json::to_string_pretty(&input).unwrap());
}
