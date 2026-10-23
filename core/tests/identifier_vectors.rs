//! Golden values for the identifiers that the escrow and every off-chain reader must derive
//! identically: the obligation id, the reference hash, the tax identifier hash and the facts
//! that the checker reads from a document.
//!
//! They moved here from the removed `ids` crate. A failure means that the commitment encoding
//! or a normalisation rule changed. That is an interpreter change and needs a new approved
//! guest image. Change a value only on purpose: in the same pull request as the encoding change,
//! with a new encoding version and a new guest image. Never change it to silence a failure.

#[path = "../examples/common/invoice.rs"]
mod invoice_fixture;

use invoice_fixture::*;
use warrant_policy::evidence::*;

fn hex32(hash: &[u8; 32]) -> String {
    let mut text = String::from("0x");
    for byte in hash {
        text.push_str(&format!("{byte:02x}"));
    }
    text
}

const OBLIGATION: &str = "0x215e64375f40ed59e78ac1b814b7e714f17da11d966446a8587e0d2f751b3a21";
const REFERENCE_PO_77: &str = "0x647b78c11a5e9b06e7088f5da15e149b7130a938d26b8c62d7256da8625788da";
const TAX_ID: &str = "0xa3f77f2f4e2df80d6836dd2e5738d95b660dc2938d9b7c20fff3f262bb9ed09c";
const DOCUMENT_HASH: &str = "0xd4dce5b8dd8330c53e28dfa2bf42a711c3aaead2bca03e02ddba1a3871726f95";

#[test]
fn obligation_id_is_pinned() {
    assert_eq!(
        hex32(&obligation_id("US-12-3456789", "INV-1001")),
        OBLIGATION
    );
}

#[test]
fn obligation_id_ignores_case_spacing_and_padding() {
    assert_eq!(
        hex32(&obligation_id(" us 12 3456789 ", " INV-1001 ")),
        OBLIGATION
    );
}

#[test]
fn reference_hash_is_pinned() {
    assert_eq!(hex32(&reference_hash("PO-77")), REFERENCE_PO_77);
}

#[test]
fn tax_id_hash_is_pinned() {
    assert_eq!(hex32(&tax_id_hash("US-12-3456789")), TAX_ID);
}

#[test]
fn facts_read_from_the_fixture_document_are_pinned() {
    let facts = parse_invoice(&doc().xml()).unwrap();
    assert_eq!(hex32(&facts.doc_hash), DOCUMENT_HASH);
    assert_eq!(facts.invoice_number, "INV-1001");
    assert_eq!(facts.seller_tax_id, "US-12-3456789");
    assert_eq!(facts.po_ref.as_deref(), Some("PO-77"));
    assert_eq!(facts.currency, "USD");
    assert_eq!(facts.payable_minor, Some(132_000));
    assert!(facts.currency_consistent && facts.precise && facts.totals_consistent);
    assert_eq!(
        hex32(&obligation_id(&facts.seller_tax_id, &facts.invoice_number)),
        OBLIGATION
    );
    assert_eq!(hex32(&tax_id_hash(&facts.seller_tax_id)), TAX_ID);
}

#[test]
fn a_document_that_is_not_an_invoice_is_denied() {
    assert!(parse_invoice(b"<NotAnInvoice/>").is_err());
}
