//! Identifiers that off-chain connectors need to join chain events to documents.
//! Every function delegates to `warrant-policy`, so a connector never
//! reimplements the bincode commitment encoding.

use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha3::{Digest, Keccak256};
use warrant_policy::evidence::{obligation_id, parse_invoice, reference_hash, tax_id_hash};
use warrant_policy::{Address, Hash};

pub fn hex32(h: &Hash) -> String {
    format!("0x{}", hex::encode(h))
}

pub fn parse_hash(text: &str) -> Result<Hash> {
    let raw = hex::decode(text.strip_prefix("0x").unwrap_or(text)).context("Hash must be hex")?;
    raw.try_into()
        .map_err(|_| anyhow::anyhow!("Hash must be 32 bytes"))
}

pub fn parse_address(text: &str) -> Result<Address> {
    let raw =
        hex::decode(text.strip_prefix("0x").unwrap_or(text)).context("Address must be hex")?;
    raw.try_into()
        .map_err(|_| anyhow::anyhow!("Address must be 20 bytes"))
}

/// `InvoiceEscrow.orderIdFor`: keccak256(abi.encode(chainId, escrow, customer, policyHash, poId)).
pub fn order_id(
    chain_id: u64,
    escrow: &Address,
    customer: &Address,
    policy_hash: &Hash,
    po_id: &Hash,
) -> Hash {
    let mut h = Keccak256::new();
    h.update([0u8; 24]);
    h.update(chain_id.to_be_bytes());
    h.update([0u8; 12]);
    h.update(escrow);
    h.update([0u8; 12]);
    h.update(customer);
    h.update(policy_hash);
    h.update(po_id);
    h.finalize().into()
}

/// Facts a connector needs from one received document. A document that the
/// checker would deny structurally is an error.
pub fn facts(document: &[u8]) -> Result<Value> {
    let f = match parse_invoice(document) {
        Ok(f) => f,
        Err(denial) => bail!("Document denied: {denial:?}"),
    };
    let po_id = f.po_ref.as_deref().map(|r| hex32(&reference_hash(r)));
    Ok(json!({
        "documentHash": hex32(&f.doc_hash),
        "invoiceNumber": f.invoice_number,
        "sellerTaxId": f.seller_tax_id,
        "taxIdHash": hex32(&tax_id_hash(&f.seller_tax_id)),
        "poRef": f.po_ref,
        "poId": po_id,
        "obligationId": hex32(&obligation_id(&f.seller_tax_id, &f.invoice_number)),
        "usd": f.usd,
        "payable": f.payable,
        "totalsConsistent": f.totals_consistent,
    }))
}
