use anyhow::{bail, ensure, Context, Result};
use risc0_zkvm::sha::{Digest, Digestible};
use risc0_zkvm::{
    default_executor, default_prover, ExecutorEnv, InnerReceipt, ProverOpts, Receipt,
};
use std::{fs, io::Write, path::Path, time::Instant};
use warrant_methods::{
    WARRANT_GUEST_ELF, WARRANT_GUEST_ID, WARRANT_INVOICE_GUEST_ELF, WARRANT_INVOICE_GUEST_ID,
};
use warrant_policy::evidence::{
    authorize_invoice, sign_journal, InvoiceAuthorization, InvoiceInput, InvoiceOutcome,
};
use warrant_policy::{authorize, Input};

/// Journal length identifies which pinned interpreter a receipt must verify against.
fn image_for(journal: &[u8]) -> Result<[u32; 8]> {
    match journal.len() {
        384 => Ok(WARRANT_GUEST_ID),
        448 => Ok(WARRANT_INVOICE_GUEST_ID),
        _ => bail!("Unexpected journal schema"),
    }
}

fn read_invoice(path: &str) -> Result<InvoiceInput> {
    // JSON encodes the document bytes as numbers, several bytes per document byte.
    let bytes = fs::read(path)?;
    ensure!(
        bytes.len() <= 4 * 1024 * 1024,
        "Invoice input exceeds the 4 MiB host limit"
    );
    Ok(serde_json::from_slice(&bytes)?)
}

fn allowed_invoice(input: &InvoiceInput) -> Result<InvoiceAuthorization> {
    match authorize_invoice(input)? {
        InvoiceOutcome::Allow(authorization) => Ok(*authorization),
        InvoiceOutcome::Ask(reasons) => bail!("Owner decision required: {reasons:?}"),
    }
}

fn read_input(path: &str) -> Result<Input> {
    let bytes = fs::read(path)?;
    ensure!(
        bytes.len() <= 64 * 1024,
        "Input exceeds the 64 KiB host limit"
    );
    Ok(serde_json::from_slice(&bytes)?)
}

fn segment_po2() -> Result<u32> {
    // Smaller segments bound peak prover memory without changing guest semantics.
    let segment_po2: u32 = std::env::var("WARRANT_SEGMENT_PO2")
        .unwrap_or_else(|_| "18".into())
        .parse()
        .context("WARRANT_SEGMENT_PO2 must be an integer")?;
    ensure!(
        (16..=20).contains(&segment_po2),
        "WARRANT_SEGMENT_PO2 must be between 16 and 20"
    );
    Ok(segment_po2)
}

fn env(input: &Input) -> Result<ExecutorEnv<'_>> {
    Ok(ExecutorEnv::builder()
        .segment_limit_po2(segment_po2()?)
        .write(input)?
        .build()?)
}

fn invoice_env(input: &InvoiceInput) -> Result<ExecutorEnv<'_>> {
    Ok(ExecutorEnv::builder()
        .segment_limit_po2(segment_po2()?)
        .write(input)?
        .build()?)
}

fn main() -> Result<()> {
    // Fake receipts must never be presented as proofs, even in a local demo.
    ensure!(
        std::env::var_os("RISC0_DEV_MODE").is_none(),
        "Unset RISC0_DEV_MODE; fake receipts are prohibited"
    );
    let args: Vec<String> = std::env::args().collect();
    ensure!(
        args.len() >= 2 && (args[1].ends_with("image-id") || args.len() >= 3),
        "Usage: warrant-host [invoice-]image-id | [invoice-]evaluate|execute|prove input.json [receipt.bin] | invoice-sign signer-key.hex input.json output.json | verify receipt.bin | wrap receipt.bin output.bin | export-evm receipt.bin output.json"
    );
    match args[1].as_str() {
        "image-id" => println!(
            "0x{}",
            hex::encode(Digest::from(WARRANT_GUEST_ID).as_bytes())
        ),
        "invoice-image-id" => println!(
            "0x{}",
            hex::encode(Digest::from(WARRANT_INVOICE_GUEST_ID).as_bytes())
        ),
        "invoice-sign" => {
            // Signer mode: the buyer-run service evaluates natively and signs the
            // journal for InvoiceEscrow.settleSigned. The key never leaves this host.
            let input_path = args.get(3).context("Missing input path")?;
            let output = args.get(4).context("Missing output path")?;
            ensure!(!Path::new(output).exists(), "Output already exists");
            let text = fs::read_to_string(&args[2]).context("Cannot read signer key file")?;
            let raw = hex::decode(text.trim().strip_prefix("0x").unwrap_or(text.trim()))
                .context("Signer key must be hex")?;
            let key = k256::ecdsa::SigningKey::from_slice(&raw).context("Invalid signer key")?;
            let input = read_invoice(input_path)?;
            let auth = allowed_invoice(&input)?;
            let journal = auth.journal();
            let scope = &auth.authorization.request.scope;
            let signature = sign_journal(&key, scope.chain_id, &scope.vault, &journal);
            let public = key.verifying_key().to_encoded_point(false);
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output)?
                .write_all(&serde_json::to_vec_pretty(&serde_json::json!({
                    "journal": format!("0x{}", hex::encode(&journal)),
                    "signature": format!("0x{}", hex::encode(&signature)),
                    "signerPublicKey": format!("0x{}", hex::encode(&public.as_bytes()[1..])),
                    "chainId": scope.chain_id,
                    "escrow": format!("0x{}", hex::encode(scope.vault)),
                    "amount": auth.authorization.request.amount,
                }))?)?;
            println!(
                "Signed authorization written to {output}; amount {}",
                auth.authorization.request.amount
            );
        }
        "invoice-evaluate" => {
            let auth = allowed_invoice(&read_invoice(&args[2])?)?;
            println!("{}", serde_json::to_string_pretty(&auth)?);
        }
        "invoice-execute" => {
            let input = read_invoice(&args[2])?;
            let expected = allowed_invoice(&input)?;
            let session =
                default_executor().execute(invoice_env(&input)?, WARRANT_INVOICE_GUEST_ELF)?;
            ensure!(
                session.journal.bytes == expected.journal(),
                "Journal mismatch"
            );
            println!(
                "Invoice guest execution succeeded (not a proof); {} cycles. Image ID: {:?}",
                session.cycles(),
                WARRANT_INVOICE_GUEST_ID
            );
        }
        "invoice-prove" => {
            let output = args.get(3).context("Missing output receipt path")?;
            ensure!(!Path::new(output).exists(), "Output already exists");
            let input = read_invoice(&args[2])?;
            let expected = allowed_invoice(&input)?;
            let started = Instant::now();
            eprintln!(
                "Generating a real succinct proof; invoice interpreter image: {:?}",
                WARRANT_INVOICE_GUEST_ID
            );
            let info = default_prover().prove_with_opts(
                invoice_env(&input)?,
                WARRANT_INVOICE_GUEST_ELF,
                &ProverOpts::succinct(),
            )?;
            ensure!(
                matches!(info.receipt.inner, InnerReceipt::Succinct(_)),
                "Expected a real succinct receipt"
            );
            info.receipt.verify(WARRANT_INVOICE_GUEST_ID)?;
            ensure!(
                info.receipt.journal.bytes == expected.journal(),
                "Journal mismatch"
            );
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output)?
                .write_all(&bincode::serialize(&info.receipt)?)?;
            println!(
                "Real succinct receipt verified; {} guest cycles; {} segments; {:.2}s elapsed",
                info.stats.total_cycles,
                info.stats.segments,
                started.elapsed().as_secs_f64()
            );
            println!("Image ID: {:?}", WARRANT_INVOICE_GUEST_ID);
            println!(
                "Policy hash: {}",
                hex::encode(expected.authorization.policy_hash)
            );
            println!("Receipt: {output}");
        }
        "evaluate" => {
            let auth = authorize(&read_input(&args[2])?)?;
            println!("{}", serde_json::to_string_pretty(&auth)?);
        }
        "execute" => {
            let input = read_input(&args[2])?;
            let session = default_executor().execute(env(&input)?, WARRANT_GUEST_ELF)?;
            ensure!(
                session.journal.bytes == authorize(&input)?.journal(),
                "Journal mismatch"
            );
            println!(
                "Guest execution succeeded (not a proof). Image ID: {:?}",
                WARRANT_GUEST_ID
            );
        }
        "prove" => {
            let output = args.get(3).context("Missing output receipt path")?;
            ensure!(!Path::new(output).exists(), "Output already exists");
            let input = read_input(&args[2])?;
            let expected = authorize(&input)?;
            let started = Instant::now();
            eprintln!(
                "Generating a real succinct proof; interpreter image: {:?}",
                WARRANT_GUEST_ID
            );
            let info = default_prover().prove_with_opts(
                env(&input)?,
                WARRANT_GUEST_ELF,
                &ProverOpts::succinct(),
            )?;
            ensure!(
                matches!(info.receipt.inner, InnerReceipt::Succinct(_)),
                "Expected a real succinct receipt"
            );
            info.receipt.verify(WARRANT_GUEST_ID)?;
            ensure!(
                info.receipt.journal.bytes == expected.journal(),
                "Journal mismatch"
            );
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output)?
                .write_all(&bincode::serialize(&info.receipt)?)?;
            println!(
                "Real succinct receipt verified; {} guest cycles; {} segments; {:.2}s elapsed",
                info.stats.total_cycles,
                info.stats.segments,
                started.elapsed().as_secs_f64()
            );
            println!("Image ID: {:?}", WARRANT_GUEST_ID);
            println!("Policy hash: {}", hex::encode(expected.policy_hash));
            println!("Receipt: {output}");
        }
        "wrap" | "export-evm" => {
            let output = args.get(3).context("Missing output path")?;
            ensure!(!Path::new(output).exists(), "Output already exists");
            let receipt: Receipt = bincode::deserialize(&fs::read(&args[2])?)?;
            ensure!(
                !matches!(receipt.inner, InnerReceipt::Fake(_)),
                "Refusing fake proof"
            );
            let image = image_for(&receipt.journal.bytes)?;
            receipt.verify(image)?;
            let bytes = if args[1] == "wrap" {
                eprintln!("Compressing verified receipt to Groth16; local Docker prover required");
                let wrapped = default_prover().compress(&ProverOpts::groth16(), &receipt)?;
                ensure!(
                    matches!(wrapped.inner, InnerReceipt::Groth16(_)),
                    "Expected Groth16 receipt"
                );
                wrapped.verify(image)?;
                ensure!(
                    wrapped.journal.bytes == receipt.journal.bytes,
                    "Journal mismatch"
                );
                bincode::serialize(&wrapped)?
            } else {
                let InnerReceipt::Groth16(ref groth16) = receipt.inner else {
                    bail!("EVM export requires a real Groth16 receipt; run wrap first");
                };
                // RISC Zero's EVM wire format: four-byte verifier-parameter selector + seal.
                let mut seal = groth16.verifier_parameters.as_bytes()[..4].to_vec();
                seal.extend_from_slice(&groth16.seal);
                serde_json::to_vec_pretty(&serde_json::json!({
                    "imageId": format!("0x{}", hex::encode(Digest::from(image).as_bytes())),
                    "journal": format!("0x{}", hex::encode(&receipt.journal.bytes)),
                    "journalDigest": format!("0x{}", hex::encode(receipt.journal.digest().as_bytes())),
                    "seal": format!("0x{}", hex::encode(seal)),
                }))?
            };
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output)?
                .write_all(&bytes)?;
            println!("Verified output written to {output}");
        }
        "verify" => {
            let receipt: Receipt = bincode::deserialize(&fs::read(&args[2])?)?;
            ensure!(
                !matches!(receipt.inner, InnerReceipt::Fake(_)),
                "Refusing fake proof"
            );
            receipt.verify(image_for(&receipt.journal.bytes)?)?;
            println!("Receipt verified against the locally pinned interpreter image ID.");
            println!("Check journal policy hash and live vault authorization before payment.");
        }
        other => bail!("Unknown command: {other}"),
    }
    Ok(())
}
