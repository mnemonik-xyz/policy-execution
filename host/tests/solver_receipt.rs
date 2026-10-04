use risc0_zkvm::{InnerReceipt, Receipt};
use std::{fs, path::PathBuf};
use warrant_methods::WARRANT_SOLVER_GUEST_ID;
use warrant_policy::solver::{authorize_solver, SolverInput};

#[test]
#[ignore = "Requires a fresh real solver.receipt and input.json in WARRANT_SOLVER_RECEIPT_DIR"]
fn real_solver_receipt_binds_image_and_all_thirteen_words() {
    let root =
        PathBuf::from(std::env::var("WARRANT_SOLVER_RECEIPT_DIR").expect("receipt directory"));
    let mut receipt: Receipt =
        bincode::deserialize(&fs::read(root.join("solver.receipt")).unwrap()).unwrap();
    let input: SolverInput =
        serde_json::from_slice(&fs::read(root.join("input.json")).unwrap()).unwrap();
    assert!(!matches!(receipt.inner, InnerReceipt::Fake(_)));
    receipt.verify(WARRANT_SOLVER_GUEST_ID).unwrap();
    assert_eq!(
        receipt.journal.bytes,
        authorize_solver(&input).unwrap().journal()
    );
    for word in 0..13 {
        receipt.journal.bytes[word * 32 + 31] ^= 1;
        assert!(
            receipt.verify(WARRANT_SOLVER_GUEST_ID).is_err(),
            "unbound word {word}"
        );
        receipt.journal.bytes[word * 32 + 31] ^= 1;
    }
    let mut wrong_image = WARRANT_SOLVER_GUEST_ID;
    wrong_image[0] ^= 1;
    assert!(receipt.verify(wrong_image).is_err());
}
