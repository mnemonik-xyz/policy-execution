#![no_main]
risc0_zkvm::guest::entry!(main);

use warrant_policy::evidence::{authorize_invoice, InvoiceInput, InvoiceOutcome};

fn main() {
    let input: InvoiceInput = risc0_zkvm::guest::env::read();
    // Ask and Deny produce no journal, so no proof of either can authorize payment.
    match authorize_invoice(&input) {
        Ok(InvoiceOutcome::Allow(authorization)) => {
            risc0_zkvm::guest::env::commit_slice(&authorization.journal())
        }
        Ok(InvoiceOutcome::Ask(reasons)) => panic!("Owner decision required: {reasons:?}"),
        Err(denial) => panic!("Payment denied: {denial}"),
    }
}
