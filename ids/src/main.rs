use anyhow::{bail, Context, Result};
use std::{env, fs, process};
use warrant_ids::*;
use warrant_policy::evidence::{obligation_id, reference_hash, tax_id_hash};

const USAGE: &str = "usage:
  warrant-ids facts <document.xml>
  warrant-ids obligation <seller-tax-id> <invoice-number>
  warrant-ids reference <text>
  warrant-ids tax-id <text>
  warrant-ids order-id <chain-id> <escrow> <customer> <policy-hash> <po-id>";

fn run(args: &[String]) -> Result<String> {
    let arg = |i: usize| -> Result<&str> { args.get(i).map(String::as_str).context(USAGE) };
    let arity = |n: usize| -> Result<()> {
        if args.len() != n {
            bail!(USAGE)
        }
        Ok(())
    };
    match args.get(1).map(String::as_str) {
        Some("facts") => {
            arity(3)?;
            let document = fs::read(arg(2)?).context("Cannot read document")?;
            Ok(serde_json::to_string_pretty(&facts(&document)?)?)
        }
        Some("obligation") => {
            arity(4)?;
            Ok(hex32(&obligation_id(arg(2)?, arg(3)?)))
        }
        Some("reference") => {
            arity(3)?;
            Ok(hex32(&reference_hash(arg(2)?)))
        }
        Some("tax-id") => {
            arity(3)?;
            Ok(hex32(&tax_id_hash(arg(2)?)))
        }
        Some("order-id") => {
            arity(7)?;
            let chain_id: u64 = arg(2)?.parse().context("Chain ID must be an integer")?;
            Ok(hex32(&order_id(
                chain_id,
                &parse_address(arg(3)?)?,
                &parse_address(arg(4)?)?,
                &parse_hash(arg(5)?)?,
                &parse_hash(arg(6)?)?,
            )))
        }
        _ => bail!(USAGE),
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    match run(&args) {
        Ok(out) => println!("{out}"),
        Err(e) => {
            eprintln!("{e:#}");
            // 2 marks a document the checker denies, so a caller can tell it apart.
            process::exit(
                if args.get(1).map(String::as_str) == Some("facts")
                    && format!("{e}").starts_with("Document denied")
                {
                    2
                } else {
                    1
                },
            );
        }
    }
}
