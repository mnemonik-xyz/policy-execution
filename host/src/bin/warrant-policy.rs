//! Off-chain template instantiation. The guest consumes the resulting typed Policy unchanged.
use anyhow::{bail, ensure, Context, Result};
use serde_json::{Map, Value};
use std::{collections::BTreeSet, fs, io::Write};
use warrant_policy::{policy_hash, validate_policy, Policy};

fn read(path: &str) -> Result<Value> {
    let bytes = fs::read(path)?;
    ensure!(bytes.len() <= 64 * 1024, "JSON exceeds 64 KiB");
    Ok(serde_json::from_slice(&bytes)?)
}
fn expand(
    value: &Value,
    bindings: &Map<String, Value>,
    used: &mut BTreeSet<String>,
) -> Result<Value> {
    match value {
        Value::Object(map) if map.contains_key("$param") => {
            ensure!(
                map.len() == 1,
                "Parameter reference must contain only $param"
            );
            let name = map["$param"]
                .as_str()
                .context("Parameter name must be a string")?;
            used.insert(name.into());
            Ok(bindings
                .get(name)
                .with_context(|| format!("Missing parameter: {name}"))?
                .clone())
        }
        Value::Object(map) => Ok(Value::Object(
            map.iter()
                .map(|(k, v)| Ok((k.clone(), expand(v, bindings, used)?)))
                .collect::<Result<_>>()?,
        )),
        Value::Array(items) => Ok(Value::Array(
            items
                .iter()
                .map(|v| expand(v, bindings, used))
                .collect::<Result<_>>()?,
        )),
        _ => Ok(value.clone()),
    }
}
fn instantiate(template: Value, parameters: Value) -> Result<Policy> {
    let template = template.as_object().context("Template must be an object")?;
    ensure!(
        template.len() == 3
            && template.get("id").is_some_and(Value::is_string)
            && template.get("description").is_some_and(Value::is_string)
            && template.contains_key("rule"),
        "Invalid template schema"
    );
    let parameters = parameters
        .as_object()
        .context("Parameters must be an object")?;
    ensure!(
        parameters.len() == 2,
        "Expected exactly policy and bindings"
    );
    let mut policy = parameters
        .get("policy")
        .and_then(Value::as_object)
        .context("Missing policy parameters")?
        .clone();
    ensure!(
        !policy.contains_key("rule"),
        "Rules must come from the reviewed template"
    );
    let bindings = parameters
        .get("bindings")
        .and_then(Value::as_object)
        .context("Missing bindings")?;
    let mut used = BTreeSet::new();
    let rule = expand(&template["rule"], bindings, &mut used)?;
    ensure!(
        bindings.keys().all(|k| used.contains(k)),
        "Unused parameter (possible typo)"
    );
    policy.insert("rule".into(), rule);
    let policy: Policy = serde_json::from_value(Value::Object(policy))?;
    validate_policy(&policy)?;
    Ok(policy)
}
fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("instantiate") if args.len() == 5 => {
            let policy = instantiate(read(&args[2])?, read(&args[3])?)?;
            fs::OpenOptions::new().write(true).create_new(true).open(&args[4])?
                .write_all(&serde_json::to_vec_pretty(&policy)?)?;
            println!("0x{}", hex::encode(policy_hash(&policy)));
        }
        Some("hash") if args.len() == 3 => {
            let policy: Policy = serde_json::from_value(read(&args[2])?)?;
            validate_policy(&policy)?;
            println!("0x{}", hex::encode(policy_hash(&policy)));
        }
        _ => bail!("Usage: warrant-policy instantiate template.json parameters.json policy.json | hash policy.json"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn params() -> Value {
        let key = k256::ecdsa::SigningKey::from_slice(&[1; 32])
            .unwrap()
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();
        serde_json::json!({"policy": {"version":1,"scope":{"chain_id":31337,"vault":vec![3;20],"token":vec![4;20]},
            "valid_after":1000,"valid_until":5000,"registry_key":key,"acceptance_key":key},
            "bindings":{"max_amount":100000000,"categories":[7,9]}})
    }
    fn template() -> Value {
        serde_json::from_str(include_str!(
            "../../../templates/accepted-contractor-v1.json"
        ))
        .unwrap()
    }
    #[test]
    fn instantiation_is_typed_and_commitment_changes_with_terms() {
        let a = instantiate(template(), params()).unwrap();
        let mut changed = params();
        changed["bindings"]["max_amount"] = 50.into();
        let b = instantiate(template(), changed).unwrap();
        assert_ne!(policy_hash(&a), policy_hash(&b));
        assert_eq!(
            a.rule,
            warrant_policy::Rule::All(vec![
                warrant_policy::Rule::Accepted,
                warrant_policy::Rule::AmountAtMost(100000000),
                warrant_policy::Rule::VendorCategoryIn(vec![7, 9])
            ])
        );
    }
    #[test]
    fn deliverable_template_binds_recipient_and_hash() {
        let template = serde_json::from_str(include_str!(
            "../../../templates/accepted-deliverable-v1.json"
        ))
        .unwrap();
        let mut p = params();
        p["bindings"]["recipient"] = serde_json::json!(vec![7; 20]);
        p["bindings"]["deliverable_hash"] = serde_json::json!(vec![6; 32]);
        let policy = instantiate(template, p).unwrap();
        let warrant_policy::Rule::All(rules) = policy.rule else {
            panic!("expected conjunction")
        };
        assert_eq!(rules.len(), 5);
        assert_eq!(rules[3], warrant_policy::Rule::RecipientEquals([7; 20]));
        assert_eq!(rules[4], warrant_policy::Rule::DeliverableEquals([6; 32]));
    }

    #[test]
    fn rejects_missing_unused_invalid_and_overridden_parameters() {
        let mut p = params();
        p["bindings"].as_object_mut().unwrap().remove("max_amount");
        assert!(instantiate(template(), p).is_err());
        let mut p = params();
        p["bindings"]["typo"] = 1.into();
        assert!(instantiate(template(), p).is_err());
        let mut p = params();
        p["bindings"]["categories"] = serde_json::json!([]);
        assert!(instantiate(template(), p).is_err());
        let mut p = params();
        p["policy"]["rule"] = "accepted".into();
        assert!(instantiate(template(), p).is_err());
    }
}
