#[path = "../../core/examples/common/solver.rs"]
mod common;
use risc0_zkvm::{default_executor, ExecutorEnv};
use warrant_methods::WARRANT_SOLVER_GUEST_ELF;
use warrant_policy::solver::{authorize_solver, SolverInput};

fn execute(input: &SolverInput) -> anyhow::Result<Vec<u8>> {
    let env = ExecutorEnv::builder()
        .segment_limit_po2(18)
        .write(input)?
        .build()?;
    Ok(default_executor()
        .execute(env, WARRANT_SOLVER_GUEST_ELF)?
        .journal
        .bytes)
}

#[test]
fn solver_guest_matches_native_without_reviewer() {
    let mut input = common::fixture();
    assert_eq!(
        execute(&input).unwrap(),
        authorize_solver(&input).unwrap().journal()
    );
    input.policy.max_cost = 15;
    input.request.amount -= 1;
    assert_eq!(
        execute(&input).unwrap(),
        authorize_solver(&input).unwrap().journal()
    );
}

#[test]
fn solver_guest_rejects_invalid_schedule_and_substituted_instance() {
    let mut input = common::fixture();
    input.schedule[2].start = 3;
    assert!(execute(&input).is_err());
    let mut input = common::fixture();
    input.instance.machines[1].price_per_unit_tick = 2;
    assert!(execute(&input).is_err());
}
