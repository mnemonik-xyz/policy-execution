#![no_main]
risc0_zkvm::guest::entry!(main);

fn main() {
    let input: warrant_policy::Input = risc0_zkvm::guest::env::read();
    let authorization = warrant_policy::authorize(&input).expect("Payment denied");
    risc0_zkvm::guest::env::commit_slice(&authorization.journal());
}
