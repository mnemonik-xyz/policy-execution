use warrant_policy::solver::*;
use warrant_policy::{Request, Rule, Scope};

/// Synthetic workload for protocol tests, not measured customer data.
pub fn fixture() -> SolverInput {
    let instance = Instance {
        machines: vec![
            Machine {
                capacity: 2,
                price_per_unit_tick: 3,
            },
            Machine {
                capacity: 2,
                price_per_unit_tick: 1,
            },
        ],
        jobs: vec![
            Job {
                units: 1,
                durations: vec![2, 4],
                release: 0,
                deadline: 20,
                dependencies: vec![],
            },
            Job {
                units: 1,
                durations: vec![2, 4],
                release: 0,
                deadline: 20,
                dependencies: vec![],
            },
            Job {
                units: 2,
                durations: vec![2, 3],
                release: 0,
                deadline: 20,
                dependencies: vec![0, 1],
            },
        ],
    };
    let schedule = vec![
        Assignment {
            job: 0,
            machine: 1,
            start: 0,
        },
        Assignment {
            job: 1,
            machine: 1,
            start: 0,
        },
        Assignment {
            job: 2,
            machine: 1,
            start: 4,
        },
    ];
    let scope = Scope {
        chain_id: 31337,
        vault: [3; 20],
        token: [4; 20],
    };
    let policy = SolverPolicy {
        version: 1,
        scope: scope.clone(),
        valid_after: 1000,
        valid_until: 5000,
        checker_version: CHECKER_VERSION,
        instance_hash: instance_hash(&instance),
        max_cost: 14,
        rule: Rule::All(vec![
            Rule::Accepted,
            Rule::AmountAtMost(10_000_000),
            Rule::RecipientEquals([5; 20]),
        ]),
    };
    let request = Request {
        scope,
        task_id: [6; 32],
        deliverable_hash: result_hash(&schedule).unwrap(),
        recipient: [5; 20],
        amount: 10_000_000,
    };
    SolverInput {
        policy,
        request,
        instance,
        schedule,
    }
}
