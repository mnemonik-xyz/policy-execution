//! Issuer-side signing utility. The agent/prover does not need the issuer's secret key.
use anyhow::{bail, ensure, Context, Result};
use k256::ecdsa::{signature::Signer, Signature, SigningKey};
use std::{fs, io::Write};
use warrant_policy::evidence::{
    invoice_attestation_message, parse_invoice, reference_hash, tax_id_hash,
    validate_invoice_policy, InvoiceAttestation, InvoicePolicy, PurchaseOrder,
};
use warrant_policy::{acceptance_message, vendor_message, Acceptance, VendorCredential};

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("prepare-invoice") {
        ensure!(args.len() == 6,
            "Usage: warrant-evidence prepare-invoice policy.json po.json invoice.xml statement.json");
        let policy_bytes = fs::read(&args[2])?;
        let po_bytes = fs::read(&args[3])?;
        ensure!(
            policy_bytes.len() <= 256 * 1024 && po_bytes.len() <= 64 * 1024,
            "Policy or PO exceeds the input limit"
        );
        let policy: InvoicePolicy = serde_json::from_slice(&policy_bytes)?;
        let po: PurchaseOrder = serde_json::from_slice(&po_bytes)?;
        validate_invoice_policy(&policy)?;
        let facts = parse_invoice(&fs::read(&args[4])?)?;
        ensure!(
            po.scope == policy.scope && po.max_total > 0 && po.max_total <= policy.max_po_total,
            "PO is outside the policy's payment domain or ceiling"
        );
        ensure!(
            facts.po_ref.as_deref().map(reference_hash) == Some(po.po_id)
                && tax_id_hash(&facts.seller_tax_id) == po.vendor_tax_id,
            "Invoice does not name this PO and vendor"
        );
        let valid_after = policy.valid_after.max(po.valid_after);
        let valid_until = policy.valid_until.min(po.valid_until);
        ensure!(valid_after <= valid_until, "Empty invoice validity window");
        let statement = InvoiceAttestation {
            scope: policy.scope,
            customer: policy.customer,
            po_id: po.po_id,
            document_hash: facts.doc_hash,
            valid_after,
            valid_until,
        };
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&args[5])?
            .write_all(&serde_json::to_vec_pretty(&statement)?)?;
        return Ok(());
    }
    ensure!(args.len() == 3 || args.len() == 5,
        "Usage: warrant-evidence public-key key.hex | sign-vendor|sign-acceptance|sign-invoice key.hex statement.json signature.json");
    let text = fs::read_to_string(&args[2]).context("Cannot read issuer key file")?;
    let raw = hex::decode(text.trim().strip_prefix("0x").unwrap_or(text.trim()))
        .context("Issuer key must be hex")?;
    let key = SigningKey::from_slice(&raw).context("Invalid issuer key")?;
    if args[1] == "public-key" && args.len() == 3 {
        println!(
            "{}",
            serde_json::to_string(key.verifying_key().to_encoded_point(true).as_bytes())?
        );
        return Ok(());
    }
    ensure!(args.len() == 5, "Missing statement or output file");
    let statement = fs::read(&args[3])?;
    ensure!(statement.len() <= 64 * 1024, "Statement exceeds 64 KiB");
    let message = match args[1].as_str() {
        "sign-vendor" => vendor_message(&serde_json::from_slice::<VendorCredential>(&statement)?),
        "sign-acceptance" => acceptance_message(&serde_json::from_slice::<Acceptance>(&statement)?),
        "sign-invoice" => {
            invoice_attestation_message(&serde_json::from_slice::<InvoiceAttestation>(&statement)?)
        }
        _ => bail!("Unknown signing operation"),
    };
    let signature: Signature = key.sign(&message);
    let signature = signature.normalize_s().unwrap_or(signature);
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&args[4])?
        .write_all(&serde_json::to_vec(&signature.to_bytes().to_vec())?)?;
    Ok(())
}
