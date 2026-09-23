//! Issuer-side signing utility. The agent/prover does not need the issuer's secret key.
use anyhow::{bail, ensure, Context, Result};
use k256::ecdsa::{signature::Signer, Signature, SigningKey};
use std::{fs, io::Write};
use warrant_policy::{acceptance_message, vendor_message, Acceptance, VendorCredential};
fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure!(args.len() == 3 || args.len() == 5,
        "Usage: warrant-evidence public-key key.hex | sign-vendor|sign-acceptance key.hex statement.json signature.json");
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
