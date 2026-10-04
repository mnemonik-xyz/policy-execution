#![no_main]
risc0_zkvm::guest::entry!(main);

fn main() {
    let input: warrant_policy::solver::SolverInput = risc0_zkvm::guest::env::read();
    let authorization =
        warrant_policy::solver::authorize_solver(&input).expect("Solver payment denied");
    risc0_zkvm::guest::env::commit_slice(&authorization.journal());
}
