//! The buyer-run signing service behind `warrant-host invoice-sign`.
//!
//! The agent is untrusted, so the service takes from it only what the checker
//! re-verifies: the document bytes, its claims and the signed credentials
//! (`SignRequest`). The policy is the service's own file; the order's spend,
//! signer, allowance, threshold and deadline are read from the escrow over
//! JSON-RPC at signing time. The service signs only journals the escrow would
//! settle, and never signs for an order that does not name its key.

use anyhow::{bail, ensure, Context, Result};
use k256::ecdsa::SigningKey;
use serde_json::{json, Value};
use sha3::{Digest, Keccak256};
use std::{fs, io::Write, path::Path};
use warrant_policy::evidence::{
    authorize_invoice, invoice_policy_hash, obligation_id, parse_invoice, sign_journal,
    usdc_amount, validate_invoice_policy, InvoiceOutcome, InvoicePolicy, SignRequest,
};
use warrant_policy::{Address, Hash};

// Selectors of the escrow views the service reads: cast sig "order(bytes32)".
const ORDER_SELECTOR: [u8; 4] = [0xe8, 0x01, 0x60, 0xab];
const STATE_ACCEPTED: u64 = 2;

/// The `InvoiceEscrow.Order` struct as `order(bytes32)` returns it: 14 static words.
#[derive(Debug)]
pub struct OrderView {
    pub customer: Address,
    pub recipient: Address,
    pub policy_hash: Hash,
    pub policy_version: u64,
    pub spent: u64,
    pub settle_by: u64,
    pub state: u64,
    pub signer: Address,
    pub signer_allowance: u64,
    pub signer_spent: u64,
    pub proof_threshold: u64,
    pub signer_revoked: bool,
}

fn word_u64(words: &[u8], i: usize) -> Result<u64> {
    let w = &words[i * 32..i * 32 + 32];
    ensure!(
        w[..24].iter().all(|b| *b == 0),
        "Word {i} does not fit in u64"
    );
    Ok(u64::from_be_bytes(w[24..].try_into().unwrap()))
}

fn word_address(words: &[u8], i: usize) -> Result<Address> {
    let w = &words[i * 32..i * 32 + 32];
    ensure!(
        w[..12].iter().all(|b| *b == 0),
        "Word {i} is not an address"
    );
    Ok(w[12..].try_into().unwrap())
}

impl OrderView {
    pub fn decode(words: &[u8]) -> Result<Self> {
        ensure!(
            words.len() == 14 * 32,
            "order() returned {} bytes",
            words.len()
        );
        Ok(OrderView {
            customer: word_address(words, 0)?,
            recipient: word_address(words, 1)?,
            policy_hash: words[64..96].try_into().unwrap(),
            policy_version: word_u64(words, 3)?,
            spent: word_u64(words, 5)?,
            settle_by: word_u64(words, 7)?,
            state: word_u64(words, 8)?,
            signer: word_address(words, 9)?,
            signer_allowance: word_u64(words, 10)?,
            signer_spent: word_u64(words, 11)?,
            proof_threshold: word_u64(words, 12)?,
            signer_revoked: word_u64(words, 13)? != 0,
        })
    }
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

/// The Ethereum address of a secp256k1 signing key.
pub fn address_of(key: &SigningKey) -> Address {
    let public = key.verifying_key().to_encoded_point(false);
    let digest = Keccak256::digest(&public.as_bytes()[1..]);
    digest[12..].try_into().unwrap()
}

fn hex(bytes: &[u8]) -> String {
    format!("0x{}", ::hex::encode(bytes))
}

/// Minimal JSON-RPC client; the escrow is read, never written, by the service.
pub struct Chain {
    url: String,
}

impl Chain {
    pub fn new(url: &str) -> Self {
        Chain { url: url.into() }
    }

    fn call(&self, method: &str, params: Value) -> Result<Value> {
        let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let mut response = ureq::post(&self.url)
            .send_json(&body)
            .with_context(|| format!("JSON-RPC {method} failed"))?;
        let reply: Value = response.body_mut().read_json()?;
        if let Some(error) = reply.get("error") {
            bail!("JSON-RPC {method} error: {error}");
        }
        reply
            .get("result")
            .cloned()
            .context("JSON-RPC reply without result")
    }

    fn quantity(&self, method: &str, params: Value) -> Result<u64> {
        let text = self.call(method, params)?;
        let text = text.as_str().context("Expected a hex quantity")?;
        Ok(u64::from_str_radix(text.trim_start_matches("0x"), 16)?)
    }

    pub fn chain_id(&self) -> Result<u64> {
        self.quantity("eth_chainId", json!([]))
    }

    pub fn latest_timestamp(&self) -> Result<u64> {
        let block = self.call("eth_getBlockByNumber", json!(["latest", false]))?;
        let ts = block["timestamp"]
            .as_str()
            .context("Block without timestamp")?;
        Ok(u64::from_str_radix(ts.trim_start_matches("0x"), 16)?)
    }

    pub fn order(&self, escrow: &Address, id: &Hash) -> Result<OrderView> {
        let mut data = ORDER_SELECTOR.to_vec();
        data.extend_from_slice(id);
        let result = self.call(
            "eth_call",
            json!([{"to": hex(escrow), "data": hex(&data)}, "latest"]),
        )?;
        let result = result.as_str().context("eth_call returned no data")?;
        let bytes = ::hex::decode(result.trim_start_matches("0x"))?;
        OrderView::decode(&bytes)
    }
}

pub struct Signed {
    pub output: Value,
    /// `None` when the checker returned Ask and nothing was signed.
    pub signature: Option<Vec<u8>>,
}

/// Evaluates an agent's request against the service's own policy and the live
/// order, and signs the journal when the checker allows the payment.
pub fn sign(
    key: &SigningKey,
    policy: InvoicePolicy,
    chain: &Chain,
    request: SignRequest,
) -> Result<Signed> {
    validate_invoice_policy(&policy).context("The service's policy is invalid")?;
    let scope = policy.scope.clone();
    ensure!(
        chain.chain_id()? == scope.chain_id,
        "RPC endpoint is not the policy's chain {}",
        scope.chain_id
    );
    let policy_hash = invoice_policy_hash(&policy);
    let id = order_id(
        scope.chain_id,
        &scope.vault,
        &policy.customer,
        &policy_hash,
        &request.po.po_id,
    );
    let order = chain.order(&scope.vault, &id)?;

    // The order must belong to the policy customer and name this service as its signer.
    ensure!(
        order.customer == policy.customer,
        "Order customer differs from the policy"
    );
    ensure!(
        order.state == STATE_ACCEPTED,
        "Order {} is not accepted",
        hex(&id)
    );
    ensure!(
        order.policy_hash == policy_hash,
        "Order policy hash differs from the service's policy"
    );
    ensure!(
        order.policy_version == policy.version,
        "Order policy version differs"
    );
    ensure!(
        order.signer == address_of(key),
        "Order {} names another signer",
        hex(&id)
    );
    ensure!(
        !order.signer_revoked,
        "The buyer revoked signing for order {}",
        hex(&id)
    );
    ensure!(
        order.recipient == request.vendor.recipient,
        "Order recipient differs from the vendor credential"
    );
    ensure!(
        chain.latest_timestamp()? <= order.settle_by,
        "Order {} is past its settlement deadline",
        hex(&id)
    );
    // Live spend from the escrow, never from the agent.
    let po_spent = order.spent;

    let facts = parse_invoice(&request.document)?;
    let obligation = obligation_id(&facts.seller_tax_id, &facts.invoice_number);
    let input = request.into_input(policy, po_spent);
    // What the buyer would approve: the payable converted at the signed order rate.
    let payable = usdc_amount(&facts, &input.po);
    match authorize_invoice(&input)? {
        InvoiceOutcome::Ask(reasons) => Ok(Signed {
            output: json!({
                "ask": reasons.iter().map(|r| format!("{r:?}")).collect::<Vec<_>>(),
                "orderId": hex(&id),
                "obligationId": hex(&obligation),
                "documentHash": hex(&facts.doc_hash),
                "payable": payable,
                "currency": facts.currency,
                "invoicePayableMinor": facts.payable_minor,
                "chainId": scope.chain_id,
                "escrow": hex(&scope.vault),
            }),
            signature: None,
        }),
        InvoiceOutcome::Allow(auth) => {
            let amount = auth.authorization.request.amount;
            // Refuse what the escrow would refuse, with a reason the operator can read.
            ensure!(
                amount < order.proof_threshold,
                "Amount {amount} is at or above the order's proof threshold {}; a proof is required",
                order.proof_threshold
            );
            ensure!(
                amount <= order.signer_allowance - order.signer_spent,
                "Amount {amount} exceeds the order's remaining signer allowance {}",
                order.signer_allowance - order.signer_spent
            );
            let journal = auth.journal();
            let signature = sign_journal(key, scope.chain_id, &scope.vault, &journal);
            let public = key.verifying_key().to_encoded_point(false);
            Ok(Signed {
                output: json!({
                    "journal": hex(&journal),
                    "signature": hex(&signature),
                    "signerPublicKey": hex(&public.as_bytes()[1..]),
                    "signer": hex(&address_of(key)),
                    "orderId": hex(&id),
                    "chainId": scope.chain_id,
                    "escrow": hex(&scope.vault),
                    "amount": amount,
                    "obligationId": hex(&obligation),
                }),
                signature: Some(signature),
            })
        }
    }
}

pub fn read_key(path: &str) -> Result<SigningKey> {
    let text = fs::read_to_string(path).context("Cannot read signer key file")?;
    let raw = ::hex::decode(text.trim().strip_prefix("0x").unwrap_or(text.trim()))
        .context("Signer key must be hex")?;
    SigningKey::from_slice(&raw).context("Invalid signer key")
}

pub fn read_policy(path: &str) -> Result<InvoicePolicy> {
    let bytes = fs::read(path).context("Cannot read policy file")?;
    ensure!(bytes.len() <= 256 * 1024, "Policy exceeds 256 KiB");
    serde_json::from_slice(&bytes).context("Policy is not a valid InvoicePolicy")
}

pub fn read_request(path: &str) -> Result<SignRequest> {
    // JSON encodes the document bytes as numbers, several bytes per document byte.
    let bytes = fs::read(path).context("Cannot read request file")?;
    ensure!(
        bytes.len() <= 4 * 1024 * 1024,
        "Request exceeds the 4 MiB limit"
    );
    serde_json::from_slice(&bytes)
        .context("Request is not a SignRequest (a policy or po_spent field is refused)")
}

pub fn write_output(path: &str, value: &Value) -> Result<()> {
    ensure!(!Path::new(path).exists(), "Output already exists");
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?
        .write_all(&serde_json::to_vec_pretty(value)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn order_id_matches_the_contract_layout() {
        // ABI words: chain 31337, escrow 0x11.., customer 0x44.., policy 0x22.., PO 0x33..
        let id = order_id(31337, &[0x11; 20], &[0x44; 20], &[0x22; 32], &[0x33; 32]);
        let mut encoded = Vec::new();
        encoded.extend_from_slice(&[0; 24]);
        encoded.extend_from_slice(&31337u64.to_be_bytes());
        encoded.extend_from_slice(&[0; 12]);
        encoded.extend_from_slice(&[0x11; 20]);
        encoded.extend_from_slice(&[0; 12]);
        encoded.extend_from_slice(&[0x44; 20]);
        encoded.extend_from_slice(&[0x22; 32]);
        encoded.extend_from_slice(&[0x33; 32]);
        assert_eq!(id, <[u8; 32]>::from(Keccak256::digest(&encoded)));
    }

    #[test]
    fn signer_address_matches_the_known_anvil_account() {
        // Anvil's fourth public test key and its address.
        let key = SigningKey::from_slice(
            &::hex::decode("7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6")
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            hex(&address_of(&key)),
            "0x90f79bf6eb2c4f870365e785982e1f101e93b906"
        );
    }

    #[test]
    fn order_view_decodes_fourteen_words() {
        let mut words = vec![0u8; 14 * 32];
        words[32 + 12..64].copy_from_slice(&[7; 20]);
        words[64..96].copy_from_slice(&[9; 32]);
        words[3 * 32 + 31] = 1;
        words[4 * 32 + 24..5 * 32].copy_from_slice(&3000u64.to_be_bytes());
        words[5 * 32 + 31] = 5;
        words[8 * 32 + 31] = 2;
        words[9 * 32 + 12..10 * 32].copy_from_slice(&[8; 20]);
        words[13 * 32 + 31] = 1;
        let o = OrderView::decode(&words).unwrap();
        assert_eq!(o.recipient, [7; 20]);
        assert_eq!(o.policy_hash, [9; 32]);
        assert_eq!((o.policy_version, o.spent, o.state), (1, 5, 2));
        assert_eq!(o.signer, [8; 20]);
        assert!(o.signer_revoked);
        assert!(OrderView::decode(&words[..13 * 32]).is_err());
    }
}
