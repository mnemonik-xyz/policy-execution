#[path = "../examples/common/solver.rs"]
mod common;
use warrant_policy::solver::*;
use warrant_policy::Rule;

fn rebind(input: &mut SolverInput) {
    input.policy.instance_hash = instance_hash(&input.instance);
    if let Ok(hash) = result_hash(&input.schedule) {
        input.request.deliverable_hash = hash;
    }
}

#[test]
fn authorizes_without_any_signature_and_binds_thirteen_words() {
    let input = common::fixture();
    let auth = authorize_solver(&input).unwrap();
    assert_eq!(auth.total_cost, 14);
    assert_eq!(auth.journal().len(), 416);
    assert_eq!(&auth.journal()[408..], &14u64.to_be_bytes());
    assert_eq!(&auth.journal()[..32], &policy_hash(&input.policy));
}

#[test]
fn missing_duplicate_reordered_and_extra_jobs_are_rejected() {
    let mut i = common::fixture();
    i.schedule.pop();
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::IncompleteSchedule
    );
    let mut i = common::fixture();
    i.schedule[1].job = 0;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidAssignment
    );
    let mut i = common::fixture();
    i.schedule.swap(0, 1);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidAssignment
    );
    let mut i = common::fixture();
    i.schedule.push(i.schedule[0].clone());
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::IncompleteSchedule
    );
}

#[test]
fn checks_exact_instance_and_exact_result() {
    let mut i = common::fixture();
    i.instance.machines[1].price_per_unit_tick = 2;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InstanceMismatch
    );
    let mut i = common::fixture();
    i.request.deliverable_hash[0] ^= 1;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::ResultMismatch
    );
}

#[test]
fn dependencies_and_deadlines_are_not_seller_claims() {
    let mut i = common::fixture();
    i.schedule[2].start = 3;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::DependencyViolation
    );
    let mut i = common::fixture();
    i.schedule[2].start = 18;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::TimingViolation
    );
    let mut i = common::fixture();
    i.instance.jobs[0].release = 1;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::TimingViolation
    );
}

#[test]
fn aggregate_capacity_and_touching_intervals() {
    let mut i = common::fixture();
    i.instance.machines[1].capacity = 1;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::CapacityExceeded
    );
    let mut i = common::fixture();
    i.schedule[1].start = 4;
    i.schedule[2].start = 8;
    rebind(&mut i);
    assert!(authorize_solver(&i).is_ok());
}

#[test]
fn cost_is_recomputed_and_exact_ceiling_is_inclusive() {
    let mut i = common::fixture();
    i.policy.max_cost = 13;
    assert_eq!(authorize_solver(&i).unwrap_err(), SolverError::CostExceeded);
    i.policy.max_cost = 14;
    assert!(authorize_solver(&i).is_ok());
}

#[test]
fn permissive_policy_cannot_bypass_the_result_checker() {
    for rule in [
        Rule::AmountAtMost(u64::MAX),
        Rule::Any(vec![Rule::Accepted, Rule::AmountAtMost(u64::MAX)]),
    ] {
        let mut i = common::fixture();
        i.policy.rule = rule;
        i.schedule[2].start = 3;
        rebind(&mut i);
        assert_eq!(
            authorize_solver(&i).unwrap_err(),
            SolverError::DependencyViolation
        );
    }
}

#[test]
fn policy_still_controls_payment_amount_and_recipient() {
    let mut i = common::fixture();
    i.request.amount += 1;
    assert_eq!(authorize_solver(&i).unwrap_err(), SolverError::PolicyDenied);
    let mut i = common::fixture();
    i.request.recipient = [9; 20];
    assert_eq!(authorize_solver(&i).unwrap_err(), SolverError::PolicyDenied);
}

#[test]
fn unsupported_authorities_and_rules_fail_closed() {
    for rule in [
        Rule::VendorCategoryIn(vec![1]),
        Rule::WithinPo,
        Rule::NoDeniedTerm,
        Rule::LineLabelsWithin(vec![1]),
        Rule::All(vec![]),
    ] {
        let mut i = common::fixture();
        i.policy.rule = rule;
        assert_eq!(
            authorize_solver(&i).unwrap_err(),
            SolverError::InvalidPolicy
        );
    }
    let mut value = serde_json::to_value(common::fixture()).unwrap();
    value["accepted"] = true.into();
    assert!(serde_json::from_value::<SolverInput>(value).is_err());
}

#[test]
fn invalid_machine_incompatible_job_and_capacity_zero_rejected() {
    let mut i = common::fixture();
    i.schedule[0].machine = 16;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidAssignment
    );
    let mut i = common::fixture();
    i.instance.jobs[0].durations[1] = 0;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidAssignment
    );
    let mut i = common::fixture();
    i.instance.machines[0].capacity = 0;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidInstance
    );
}

#[test]
fn invalid_or_cyclic_dependency_graphs_rejected() {
    for deps in [vec![2], vec![0, 0], vec![1, 0], vec![99]] {
        let mut i = common::fixture();
        i.instance.jobs[2].dependencies = deps;
        rebind(&mut i);
        assert_eq!(
            authorize_solver(&i).unwrap_err(),
            SolverError::InvalidInstance
        );
    }
}

#[test]
fn rejects_overflow_in_end_cost_and_total() {
    let mut i = common::fixture();
    i.schedule[0].start = u64::MAX;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::ArithmeticOverflow
    );
    let mut i = common::fixture();
    i.instance.machines[1].price_per_unit_tick = u64::MAX;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::ArithmeticOverflow
    );
    let mut i = common::fixture();
    i.instance.machines[1].price_per_unit_tick = u64::MAX / 10;
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::ArithmeticOverflow
    );
}

#[test]
fn oversized_inputs_and_rule_trees_rejected() {
    let mut i = common::fixture();
    i.instance.jobs = vec![i.instance.jobs[0].clone(); MAX_JOBS + 1];
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidInstance
    );
    let mut i = common::fixture();
    i.instance.machines = vec![i.instance.machines[0].clone(); MAX_MACHINES + 1];
    rebind(&mut i);
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidInstance
    );
    let mut i = common::fixture();
    for _ in 0..10 {
        i.policy.rule = Rule::All(vec![i.policy.rule]);
    }
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidPolicy
    );
}

#[test]
fn request_scope_and_policy_version_are_checked() {
    let mut i = common::fixture();
    i.request.scope.chain_id += 1;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidRequest
    );
    let mut i = common::fixture();
    i.policy.checker_version += 1;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidPolicy
    );
    let mut i = common::fixture();
    i.policy.valid_after = i.policy.valid_until + 1;
    assert_eq!(
        authorize_solver(&i).unwrap_err(),
        SolverError::InvalidPolicy
    );
}

#[test]
fn capacity_event_check_matches_discrete_time_oracle() {
    // Exhaustively compare the event sweep to an independent tick-by-tick oracle.
    for capacity in 1..=3 {
        for a in 0..=4 {
            for b in 0..=4 {
                for c in 0..=4 {
                    let instance = Instance {
                        machines: vec![Machine {
                            capacity,
                            price_per_unit_tick: 1,
                        }],
                        jobs: vec![
                            Job {
                                units: 1,
                                durations: vec![2],
                                release: 0,
                                deadline: 10,
                                dependencies: vec![]
                            };
                            3
                        ],
                    };
                    let schedule = vec![
                        Assignment {
                            job: 0,
                            machine: 0,
                            start: a,
                        },
                        Assignment {
                            job: 1,
                            machine: 0,
                            start: b,
                        },
                        Assignment {
                            job: 2,
                            machine: 0,
                            start: c,
                        },
                    ];
                    let valid = (0..10).all(|tick| {
                        [a, b, c]
                            .iter()
                            .filter(|s| **s <= tick && tick < **s + 2)
                            .count()
                            <= capacity as usize
                    });
                    assert_eq!(check_schedule(&instance, &schedule).is_ok(), valid);
                }
            }
        }
    }
}

#[test]
fn canonical_result_encoding_matches_published_format() {
    let bytes = result_bytes(&[Assignment {
        job: 0,
        machine: 1,
        start: 4,
    }])
    .unwrap();
    let mut expected = b"warrant/schedule/v1".to_vec();
    expected.extend_from_slice(&[0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 4]);
    assert_eq!(bytes, expected);
    assert_eq!(MAX_RESULT_BYTES, 2071);
}

#[test]
fn solidity_fixture_matches_native_journal_and_result() {
    fn hex(bytes: &[u8]) -> String {
        let mut text = String::from("0x");
        for byte in bytes {
            text.push_str(&format!("{byte:02x}"));
        }
        text
    }
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../contracts/test/fixtures/solver-journal.json"
    ))
    .unwrap();
    let input = common::fixture();
    let auth = authorize_solver(&input).unwrap();
    assert_eq!(fixture["journal"], hex(&auth.journal()));
    assert_eq!(
        fixture["result"],
        hex(&result_bytes(&input.schedule).unwrap())
    );
    assert_eq!(fixture["policyHash"], hex(&auth.authorization.policy_hash));
}
