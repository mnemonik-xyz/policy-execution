//! Native checker and buyer/seller artifact tool. Produces no cryptographic proof.
use serde::de::DeserializeOwned;
use std::{error::Error, fs, io::Write};
use warrant_policy::solver::{self, Instance, SolverInput, SolverPolicy};
use warrant_policy::Request;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

fn read<T: DeserializeOwned>(path: &str) -> Result<T> {
    let bytes = fs::read(path)?;
    if bytes.len() > 1024 * 1024 {
        return Err("Input exceeds 1 MiB".into());
    }
    Ok(serde_json::from_slice(&bytes)?)
}

fn hex(bytes: &[u8]) -> String {
    let mut text = String::from("0x");
    for byte in bytes {
        text.push_str(&format!("{byte:02x}"));
    }
    text
}

fn unhex<const N: usize>(value: &str) -> Result<[u8; N]> {
    let value = value.strip_prefix("0x").unwrap_or(value);
    if value.len() != N * 2 || !value.is_ascii() {
        return Err("Invalid hex length".into());
    }
    let mut bytes = [0u8; N];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[i * 2..i * 2 + 2], 16)?;
    }
    Ok(bytes)
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("instance-hash") if args.len() == 3 => {
            let instance: Instance = read(&args[2])?;
            solver::validate_instance(&instance)?;
            println!("{}", hex(&solver::instance_hash(&instance)));
        }
        Some("policy-hash") if args.len() == 3 => {
            println!("{}", hex(&solver::policy_hash(&read::<SolverPolicy>(&args[2])?)));
        }
        Some("check") if args.len() == 4 => {
            let instance: Instance = read(&args[2])?;
            let schedule = read::<Vec<solver::Assignment>>(&args[3])?;
            let cost = solver::check_schedule(&instance, &schedule)?;
            println!("{}", serde_json::json!({"totalCost":cost,"resultHash":hex(&solver::result_hash(&schedule)?),"result":hex(&solver::result_bytes(&schedule)?)}));
        }
        Some("prepare") if args.len() == 9 => {
            let policy: SolverPolicy = read(&args[2])?;
            let instance: Instance = read(&args[3])?;
            let schedule = read::<Vec<solver::Assignment>>(&args[4])?;
            let request = Request { scope:policy.scope.clone(), task_id:unhex(&args[5])?,
                recipient:unhex(&args[6])?, amount:args[7].parse()?, deliverable_hash:solver::result_hash(&schedule)? };
            let input = SolverInput {policy,request,instance,schedule};
            solver::authorize_solver(&input)?;
            fs::OpenOptions::new().write(true).create_new(true).open(&args[8])?
                .write_all(&serde_json::to_vec_pretty(&input)?)?;
            println!("{}", hex(&solver::policy_hash(&input.policy)));
        }
        Some("inspect") if args.len() == 3 => {
            let input: SolverInput = read(&args[2])?;
            let auth = solver::authorize_solver(&input)?;
            println!("{}", serde_json::to_string_pretty(&serde_json::json!({
                "authorization":auth,"policyHash":hex(&auth.authorization.policy_hash),
                "journal":hex(&auth.journal()),"result":hex(&solver::result_bytes(&input.schedule)?),
                "realProof":false
            }))?);
        }
        _ => return Err("Usage: warrant-solver instance-hash instance.json | policy-hash policy.json | check instance.json schedule.json | prepare policy.json instance.json schedule.json task-id recipient amount input.json | inspect input.json".into()),
    }
    Ok(())
}
