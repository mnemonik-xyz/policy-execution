//! Evidence checker for invoice payments (see `evidence-checker.md`).
//!
//! The agent is untrusted. It supplies the received document, per-line claims and
//! signed credentials. Facts reach the evaluator only when they are signed by an
//! owner-approved key, derived deterministically from the document bytes, or
//! backed by claim evidence this module re-checks. Everything else is unknown.

use crate::xml::{self, Element};
use crate::{
    hash_tagged, tagged_bytes, valid_key, valid_rule, valid_scope, verify, Acceptance, Address,
    Authorization, Denial, Hash, Request, Scope,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use warrant_verified_policy::{decide, Decision, Facts3, Rule};

/// Changes whenever parsing or checking semantics change; bound into evidence.
pub const CHECKER_VERSION: u32 = 3;
/// Invoice currencies the checker can read, with their ISO 4217 minor units.
/// Adding one changes the checker version and the guest image.
pub const CURRENCIES: &[(&str, u32)] = &[("USD", 2), ("EUR", 2), ("AMD", 2)];
/// One US cent is 10,000 USDC base units. A USD order must carry exactly this rate.
pub const USD_RATE: (u64, u64) = (10_000, 1);
/// Longest fraction the amount parser reads before it gives up on a value.
const MAX_FRACTION_DIGITS: usize = 18;
const MAX_LINES: usize = 256;
const MAX_TERMS: usize = 256;

const UBL_INVOICE: &str = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2";
const CAC: &str = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2";
const CBC: &str = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LexiconEntry {
    pub label: u16,
    /// Lowercase ASCII terms, matched as whole words.
    pub terms: Vec<String>,
}

/// Owner-approved policy for invoice payments. Its commitment is fixed per funded order.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvoicePolicy {
    pub version: u64,
    /// Funding customer whose orders this policy can authorize.
    pub customer: Address,
    pub scope: Scope,
    pub valid_after: u64,
    pub valid_until: u64,
    pub registry_key: Vec<u8>,
    /// The buyer's key. It signs purchase orders and nothing else.
    pub po_key: Vec<u8>,
    /// Buyer-approved invoice source or trusted intake authority, separate from settlement signing.
    pub invoice_key: Vec<u8>,
    pub acceptance_key: Option<Vec<u8>>,
    /// Bounds on what a purchase order may authorise.
    pub max_po_total: u64,
    pub po_categories: Vec<u16>,
    /// ISO 4217 codes a purchase order may name as its invoice currency.
    pub currencies: Vec<String>,
    pub lexicon: Vec<LexiconEntry>,
    pub deny_terms: Vec<String>,
    pub rule: Rule,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvoiceVendorCredential {
    pub scope: Scope,
    pub recipient: Address,
    pub category: u16,
    /// `tax_id_hash` of the vendor's tax identifier.
    pub tax_id: Hash,
    pub valid_after: u64,
    pub valid_until: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PoLine {
    /// `reference_hash` of the seller's item identifier.
    pub item_id: Hash,
    pub category: u16,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PurchaseOrder {
    pub scope: Scope,
    /// `reference_hash` of the order number the invoice must cite.
    pub po_id: Hash,
    pub vendor_tax_id: Hash,
    /// Lifetime ceiling across every invoice paid against this order, in USDC base units.
    pub max_total: u64,
    /// ISO 4217 code of the invoices this order accepts.
    pub currency: String,
    /// The buyer's contract rate: `rate_num` USDC base units per `rate_den`
    /// minor units of `currency`. The agent cannot choose it.
    pub rate_num: u64,
    pub rate_den: u64,
    pub lines: Vec<PoLine>,
    pub valid_after: u64,
    pub valid_until: u64,
}

/// An invoice source attests to exact document bytes for one buyer, PO and payment domain.
/// This authenticates the supplied record, not delivery or uniqueness of the underlying debt.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvoiceAttestation {
    pub scope: Scope,
    pub customer: Address,
    pub po_id: Hash,
    pub document_hash: Hash,
    pub valid_after: u64,
    pub valid_until: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClaimEvidence {
    /// The invoice line matches a buyer-signed order line; its category is the label.
    PoLine { po_line: u16 },
    /// Byte range in the line's decoded text containing an approved term for the label.
    Span { start: u32, end: u32 },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LineClaim {
    pub line: u16,
    pub label: u16,
    pub evidence: ClaimEvidence,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvoiceInput {
    pub policy: InvoicePolicy,
    /// The document exactly as received: UBL 2.1 Invoice XML.
    pub document: Vec<u8>,
    pub claims: Vec<LineClaim>,
    pub vendor: InvoiceVendorCredential,
    pub vendor_signature: Vec<u8>,
    pub po: PurchaseOrder,
    pub po_signature: Vec<u8>,
    /// Both absent means Ask; a partial or invalid attestation is rejected.
    pub invoice_attestation: Option<InvoiceAttestation>,
    pub invoice_signature: Option<Vec<u8>>,
    pub acceptance: Option<Acceptance>,
    pub acceptance_signature: Option<Vec<u8>>,
    /// Spend already recorded against this order, read from the vault. The vault
    /// re-checks it on chain; a stale value cannot cause an over-payment there.
    pub po_spent: u64,
}

/// What an untrusted agent may hand to the buyer-run signing service: the received
/// bytes, its claims and the signed credentials. The policy and the order's spend
/// are not accepted from the agent; the service holds the policy and reads the
/// spend from the escrow. Unknown fields are rejected, so a request that smuggles
/// either in fails to parse.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignRequest {
    pub document: Vec<u8>,
    pub claims: Vec<LineClaim>,
    pub vendor: InvoiceVendorCredential,
    pub vendor_signature: Vec<u8>,
    pub po: PurchaseOrder,
    pub po_signature: Vec<u8>,
    /// Both absent means Ask; a partial or invalid attestation is rejected.
    pub invoice_attestation: Option<InvoiceAttestation>,
    pub invoice_signature: Option<Vec<u8>>,
    pub acceptance: Option<Acceptance>,
    pub acceptance_signature: Option<Vec<u8>>,
}

impl SignRequest {
    /// Completes the request with what the service knows on its own authority.
    pub fn into_input(self, policy: InvoicePolicy, po_spent: u64) -> InvoiceInput {
        InvoiceInput {
            policy,
            document: self.document,
            claims: self.claims,
            vendor: self.vendor,
            vendor_signature: self.vendor_signature,
            po: self.po,
            po_signature: self.po_signature,
            invoice_attestation: self.invoice_attestation,
            invoice_signature: self.invoice_signature,
            acceptance: self.acceptance,
            acceptance_signature: self.acceptance_signature,
            po_spent,
        }
    }
}

impl From<InvoiceInput> for SignRequest {
    fn from(input: InvoiceInput) -> Self {
        SignRequest {
            document: input.document,
            claims: input.claims,
            vendor: input.vendor,
            vendor_signature: input.vendor_signature,
            po: input.po,
            po_signature: input.po_signature,
            invoice_attestation: input.invoice_attestation,
            invoice_signature: input.invoice_signature,
            acceptance: input.acceptance,
            acceptance_signature: input.acceptance_signature,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct InvoiceAuthorization {
    pub authorization: Authorization,
    pub po_id: Hash,
    pub po_max_total: u64,
    pub customer: Address,
}

impl InvoiceAuthorization {
    /// The 12 base journal words followed by `poId`, `poMaxTotal` and `customer`.
    pub fn journal(&self) -> Vec<u8> {
        let mut out = self.authorization.journal();
        out.extend_from_slice(&self.po_id);
        out.extend_from_slice(&[0; 24]);
        out.extend_from_slice(&self.po_max_total.to_be_bytes());
        out.extend_from_slice(&[0; 12]);
        out.extend_from_slice(&self.customer);
        out
    }
}

/// What the escrow's signer signs: `InvoiceEscrow.signerDigest(journal)`. The tag,
/// chain and escrow address bind the signature to one contract even though the
/// journal already carries them.
pub fn signer_digest(chain_id: u64, escrow: &Address, journal: &[u8]) -> Hash {
    let mut hasher = Sha256::new();
    hasher.update(b"warrant/invoice-signer/v1");
    hasher.update([0u8; 24]);
    hasher.update(chain_id.to_be_bytes());
    hasher.update(escrow);
    hasher.update(Sha256::digest(journal));
    hasher.finalize().into()
}

/// Signs `journal` for `InvoiceEscrow.settleSigned`: 65 bytes `r || s || v`, low-s,
/// `v` in {27, 28}, as OpenZeppelin's `ECDSA.recover` expects.
pub fn sign_journal(
    key: &k256::ecdsa::SigningKey,
    chain_id: u64,
    escrow: &Address,
    journal: &[u8],
) -> Vec<u8> {
    use k256::ecdsa::signature::hazmat::PrehashSigner;
    let digest = signer_digest(chain_id, escrow, journal);
    let (signature, recovery): (k256::ecdsa::Signature, k256::ecdsa::RecoveryId) = key
        .sign_prehash(&digest)
        .expect("signing cannot fail for a valid key");
    // Canonical low-s; flipping s flips the recovered point's parity.
    let (signature, recovery) = match signature.normalize_s() {
        Some(low) => (
            low,
            k256::ecdsa::RecoveryId::from_byte(recovery.to_byte() ^ 1).unwrap(),
        ),
        None => (signature, recovery),
    };
    let mut out = signature.to_bytes().to_vec();
    out.push(27 + recovery.to_byte());
    out
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AskReason {
    InvoiceAttestationMissing,
    /// The document currency is not in the checker's currency table.
    UnsupportedCurrency,
    /// An amount is not in the document currency, or the document is not in the order's.
    CurrencyMismatch,
    /// An amount has more decimals than the currency's minor units.
    CurrencyPrecision,
    TotalsInconsistent,
    PayableUnknown,
    /// Invoice line indices whose label could not be admitted.
    UnlabeledLines(Vec<u16>),
    AcceptanceMissing,
    /// The policy outcome depends on unknown facts in another way.
    Undetermined,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InvoiceOutcome {
    Allow(Box<InvoiceAuthorization>),
    /// No payment path; the owner decides.
    Ask(Vec<AskReason>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvoiceLine {
    /// In minor units of the document currency.
    pub amount: Option<i128>,
    /// Item name and descriptions, decoded and joined with newlines. Spans index this.
    pub text: String,
    pub item_id: Option<String>,
}

/// Tier 0: facts derived deterministically from the received bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvoiceFacts {
    pub doc_hash: Hash,
    pub invoice_number: String,
    pub seller_tax_id: String,
    pub po_ref: Option<String>,
    /// `DocumentCurrencyCode` exactly as written.
    pub currency: String,
    /// Every amount carries `currencyID` equal to `currency`.
    pub currency_consistent: bool,
    /// No amount has more decimals than the currency's minor units.
    pub precise: bool,
    /// In minor units of `currency`; `None` when missing, negative, too precise
    /// or the currency is unsupported. `usdc_amount` converts it at the order's rate.
    pub payable_minor: Option<u64>,
    pub totals_consistent: bool,
    pub lines: Vec<InvoiceLine>,
}

pub fn invoice_policy_hash(policy: &InvoicePolicy) -> Hash {
    hash_tagged(b"warrant/invoice-policy/v3", policy)
}

pub fn invoice_vendor_message(credential: &InvoiceVendorCredential) -> Vec<u8> {
    tagged_bytes(b"warrant/invoice-vendor/v1", credential)
}

pub fn invoice_attestation_message(attestation: &InvoiceAttestation) -> Vec<u8> {
    tagged_bytes(b"warrant/invoice-attestation/v1", attestation)
}

/// SHA-256 of the exact invoice bytes, without XML canonicalization or reformatting.
pub fn invoice_document_hash(document: &[u8]) -> Hash {
    Sha256::digest(document).into()
}

pub fn po_message(po: &PurchaseOrder) -> Vec<u8> {
    tagged_bytes(b"warrant/purchase-order/v1", po)
}

/// Tax identifiers compare on ASCII letters and digits only, uppercased.
pub fn tax_id_hash(raw: &str) -> Hash {
    let normalized: String = raw
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_uppercase())
        .collect();
    hash_tagged(b"warrant/tax-id/v1", &normalized)
}

/// Order numbers and item identifiers compare exactly after trimming whitespace.
pub fn reference_hash(raw: &str) -> Hash {
    hash_tagged(b"warrant/reference/v1", &raw.trim())
}

/// One payable obligation per seller and invoice number, whatever the agent claims.
pub fn obligation_id(seller_tax_id: &str, invoice_number: &str) -> Hash {
    hash_tagged(
        b"warrant/obligation/v1",
        &(tax_id_hash(seller_tax_id), invoice_number.trim()),
    )
}

fn valid_term(term: &str) -> bool {
    !term.is_empty()
        && term.len() <= 64
        && term
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b' ')
        && !term.starts_with(' ')
        && !term.ends_with(' ')
}

pub fn validate_invoice_policy(policy: &InvoicePolicy) -> Result<(), Denial> {
    let terms: usize = policy.lexicon.iter().map(|e| e.terms.len()).sum();
    let ok = policy.version != 0
        && policy.customer != [0; 20]
        && valid_scope(&policy.scope)
        && policy.valid_after <= policy.valid_until
        && valid_key(&policy.registry_key)
        && valid_key(&policy.po_key)
        && valid_key(&policy.invoice_key)
        && policy.acceptance_key.as_deref().is_none_or(valid_key)
        && policy.max_po_total > 0
        && !policy.po_categories.is_empty()
        && policy.po_categories.len() <= 64
        && !policy.po_categories.contains(&0)
        && !policy.currencies.is_empty()
        && policy.currencies.len() <= CURRENCIES.len()
        && policy
            .currencies
            .iter()
            .enumerate()
            .all(|(i, c)| minor_units(c).is_some() && !policy.currencies[..i].contains(c))
        && policy.lexicon.len() <= 64
        && terms + policy.deny_terms.len() <= MAX_TERMS
        && policy
            .lexicon
            .iter()
            .all(|e| e.label != 0 && !e.terms.is_empty() && e.terms.iter().all(|t| valid_term(t)))
        && policy.deny_terms.iter().all(|t| valid_term(t))
        && valid_rule(&policy.rule, true, 0, &mut 128);
    if ok {
        Ok(())
    } else {
        Err(Denial::InvalidPolicy)
    }
}

/// ISO 4217 minor units of a supported currency.
pub fn minor_units(code: &str) -> Option<u32> {
    CURRENCIES.iter().find(|(c, _)| *c == code).map(|(_, d)| *d)
}

/// The payable amount in USDC base units at the order's contract rate, rounded
/// down so the vendor never receives more than the signed rate gives. `None`
/// when the currencies differ, the amount is unknown, or the result is zero or
/// does not fit a `u64`.
pub fn usdc_amount(facts: &InvoiceFacts, po: &PurchaseOrder) -> Option<u64> {
    if facts.currency != po.currency || po.rate_den == 0 {
        return None;
    }
    let minor = u128::from(facts.payable_minor?);
    let value = minor * u128::from(po.rate_num) / u128::from(po.rate_den);
    u64::try_from(value).ok().filter(|v| *v > 0)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Amount {
    /// In minor units.
    Value(i128),
    /// Well formed, but with more decimals than the currency allows.
    TooPrecise,
    Invalid,
}

/// Exact decimal to minor units of a currency with `decimals` minor units.
/// Exponents, signs other than a leading minus and other forms are invalid.
/// Trailing zeros count: EN 16931 allows at most the currency's decimals.
fn parse_amount(raw: &str, decimals: u32) -> Amount {
    let raw = raw.trim();
    let (negative, digits) = match raw.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, raw),
    };
    let (whole, frac) = digits.split_once('.').unwrap_or((digits, ""));
    if whole.is_empty()
        || whole.len() > 18
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || frac.len() > MAX_FRACTION_DIGITS
        || (digits.contains('.') && frac.is_empty())
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return Amount::Invalid;
    }
    if frac.len() > decimals as usize {
        return Amount::TooPrecise;
    }
    let Ok(mut value) = whole.parse::<i128>() else {
        return Amount::Invalid;
    };
    for b in frac.bytes() {
        value = value * 10 + i128::from(b - b'0');
    }
    value *= 10i128.pow(decimals - frac.len() as u32);
    Amount::Value(if negative { -value } else { value })
}

fn text_of(el: &Element) -> String {
    el.text.trim().to_string()
}

fn cbc_text(parent: &Element, local: &str) -> Result<Option<String>, Denial> {
    Ok(parent
        .child(CBC, local)
        .map_err(|_| Denial::InvalidDocument)?
        .map(text_of))
}

fn cac<'a>(parent: &'a Element, local: &str) -> Result<Option<&'a Element>, Denial> {
    parent
        .child(CAC, local)
        .map_err(|_| Denial::InvalidDocument)
}

/// Parses the received bytes into Tier 0 facts. Structural problems deny; values
/// the checker cannot establish become unknown and resolve to Ask later.
pub fn parse_invoice(document: &[u8]) -> Result<InvoiceFacts, Denial> {
    let doc_hash = invoice_document_hash(document);
    let root = xml::parse(document).map_err(|_| Denial::InvalidDocument)?;
    if root.ns != UBL_INVOICE || root.local != "Invoice" {
        return Err(Denial::InvalidDocument);
    }
    let invoice_number = cbc_text(&root, "ID")?.filter(|s| !s.is_empty() && s.len() <= 64);
    let invoice_number = invoice_number.ok_or(Denial::InvalidDocument)?;
    let currency = cbc_text(&root, "DocumentCurrencyCode")?.unwrap_or_default();

    let supplier = cac(&root, "AccountingSupplierParty")?
        .map(|s| cac(s, "Party"))
        .transpose()?
        .flatten()
        .ok_or(Denial::InvalidDocument)?;
    let mut schemes = supplier.children(CAC, "PartyTaxScheme");
    let scheme = schemes.next().ok_or(Denial::InvalidDocument)?;
    if schemes.next().is_some() {
        return Err(Denial::InvalidDocument);
    }
    let seller_tax_id = cbc_text(scheme, "CompanyID")?
        .filter(|s| s.chars().any(|c| c.is_ascii_alphanumeric()))
        .ok_or(Denial::InvalidDocument)?;
    let po_ref = cac(&root, "OrderReference")?
        .map(|o| cbc_text(o, "ID"))
        .transpose()?
        .flatten()
        .filter(|s| !s.is_empty());

    // Every monetary amount must carry the document currency and fit its minor
    // units. An unsupported currency is reported by the caller before totals matter.
    let decimals = minor_units(&currency).unwrap_or(2);
    let mut currency_consistent = true;
    let mut precise = true;
    let mut amount = |el: Option<&Element>| -> Option<i128> {
        let el = el?;
        if el.attr("currencyID") != Some(currency.as_str()) {
            currency_consistent = false;
        }
        match parse_amount(&el.text, decimals) {
            Amount::Value(v) => Some(v),
            Amount::TooPrecise => {
                precise = false;
                None
            }
            Amount::Invalid => None,
        }
    };

    let total = cac(&root, "LegalMonetaryTotal")?.ok_or(Denial::InvalidDocument)?;
    let get = |local: &str| total.child(CBC, local).map_err(|_| Denial::InvalidDocument);
    let line_sum = amount(get("LineExtensionAmount")?);
    let tax_exclusive = amount(get("TaxExclusiveAmount")?);
    let tax_inclusive = amount(get("TaxInclusiveAmount")?);
    let allowances = get("AllowanceTotalAmount")?.map_or(Some(0), |e| amount(Some(e)));
    let charges = get("ChargeTotalAmount")?.map_or(Some(0), |e| amount(Some(e)));
    let prepaid = get("PrepaidAmount")?.map_or(Some(0), |e| amount(Some(e)));
    let rounding = get("PayableRoundingAmount")?.map_or(Some(0), |e| amount(Some(e)));
    let payable_raw = amount(get("PayableAmount")?);

    // Document-level tax in the document currency; at most one such total.
    let mut tax: Option<i128> = Some(0);
    let mut tax_totals = 0;
    for tt in root.children(CAC, "TaxTotal") {
        let el = tt
            .child(CBC, "TaxAmount")
            .map_err(|_| Denial::InvalidDocument)?;
        if el.and_then(|e| e.attr("currencyID")) == Some(currency.as_str()) {
            tax_totals += 1;
            tax = amount(el);
        }
    }
    if tax_totals > 1 {
        return Err(Denial::InvalidDocument);
    }

    let mut lines = Vec::new();
    let mut sum: Option<i128> = Some(0);
    for line in root.children(CAC, "InvoiceLine") {
        if lines.len() == MAX_LINES {
            return Err(Denial::InvalidDocument);
        }
        let item = cac(line, "Item")?.ok_or(Denial::InvalidDocument)?;
        let mut text = item
            .child(CBC, "Name")
            .map_err(|_| Denial::InvalidDocument)?
            .map(|e| e.text.clone())
            .unwrap_or_default();
        for d in item.children(CBC, "Description") {
            text.push('\n');
            text.push_str(&d.text);
        }
        let item_id = cac(item, "SellersItemIdentification")?
            .map(|s| cbc_text(s, "ID"))
            .transpose()?
            .flatten()
            .filter(|s| !s.is_empty());
        let line_amount = amount(
            line.child(CBC, "LineExtensionAmount")
                .map_err(|_| Denial::InvalidDocument)?,
        );
        sum = sum.zip(line_amount).and_then(|(a, b)| a.checked_add(b));
        lines.push(InvoiceLine {
            amount: line_amount,
            text,
            item_id,
        });
    }
    if lines.is_empty() {
        return Err(Denial::InvalidDocument);
    }

    // Straight sums without rounding tolerance; mismatch resolves to Ask, not Deny.
    let totals_consistent = (|| {
        let (sum, line_sum, excl, incl) = (sum?, line_sum?, tax_exclusive?, tax_inclusive?);
        let payable = payable_raw?;
        Some(
            sum == line_sum
                && excl == line_sum - allowances? + charges?
                && incl == excl + tax?
                && payable == incl - prepaid? + rounding?,
        )
    })()
    .unwrap_or(false);
    let payable_minor = payable_raw
        .filter(|v| *v > 0 && minor_units(&currency).is_some())
        .and_then(|v| u64::try_from(v).ok());

    Ok(InvoiceFacts {
        doc_hash,
        invoice_number,
        seller_tax_id,
        po_ref,
        currency,
        currency_consistent,
        precise,
        payable_minor,
        totals_consistent,
        lines,
    })
}

fn ascii_fold(s: &str) -> Vec<u8> {
    s.bytes().map(|b| b.to_ascii_lowercase()).collect()
}

fn is_word(b: u8) -> bool {
    b.is_ascii_alphanumeric()
}

/// Whole-word occurrence of `term` whose bytes lie within `[start, end)` of `text`.
/// Word boundaries are judged against the full text, not the span.
fn contains_word(text: &[u8], start: usize, end: usize, term: &str) -> bool {
    let term = term.as_bytes();
    if term.is_empty() || end < start + term.len() {
        return false;
    }
    (start..=end - term.len()).any(|i| {
        text[i..i + term.len()] == *term
            && (i == 0 || !is_word(text[i - 1]))
            && (i + term.len() == text.len() || !is_word(text[i + term.len()]))
    })
}

/// Tier 1: admit each claim only if its evidence re-checks. Invalid claims become
/// unknown labels rather than errors, so a confused model at worst causes Ask.
pub fn admit_labels(
    facts: &InvoiceFacts,
    claims: &[LineClaim],
    po: &PurchaseOrder,
    lexicon: &[LexiconEntry],
) -> Result<Vec<Option<u16>>, Denial> {
    if claims.len() > MAX_LINES {
        return Err(Denial::InvalidRequest);
    }
    let mut labels = vec![None; facts.lines.len()];
    let mut claimed = vec![false; facts.lines.len()];
    for claim in claims {
        let i = usize::from(claim.line);
        if i >= facts.lines.len() || claimed[i] {
            return Err(Denial::InvalidRequest);
        }
        claimed[i] = true;
        let line = &facts.lines[i];
        let admitted = claim.label != 0
            && match &claim.evidence {
                ClaimEvidence::PoLine { po_line } => po
                    .lines
                    .get(usize::from(*po_line))
                    .zip(line.item_id.as_deref())
                    .is_some_and(|(p, id)| {
                        p.item_id == reference_hash(id) && p.category == claim.label
                    }),
                ClaimEvidence::Span { start, end } => {
                    let (start, end) = (*start as usize, *end as usize);
                    start < end
                        && end <= line.text.len()
                        && line.text.is_char_boundary(start)
                        && line.text.is_char_boundary(end)
                        && lexicon.iter().filter(|e| e.label == claim.label).any(|e| {
                            let folded = ascii_fold(&line.text);
                            e.terms
                                .iter()
                                .any(|t| contains_word(&folded, start, end, t))
                        })
                }
            };
        if admitted {
            labels[i] = Some(claim.label);
        }
    }
    Ok(labels)
}

/// Deterministic scan of every line; the agent cannot suppress a hit by silence.
pub fn denied_term_found(facts: &InvoiceFacts, deny_terms: &[String]) -> bool {
    facts.lines.iter().any(|line| {
        let folded = ascii_fold(&line.text);
        deny_terms
            .iter()
            .any(|t| contains_word(&folded, 0, folded.len(), t))
    })
}

fn mentions_accepted(rule: &Rule) -> bool {
    match rule {
        Rule::All(children) | Rule::Any(children) => children.iter().any(mentions_accepted),
        Rule::Accepted => true,
        _ => false,
    }
}

pub fn authorize_invoice(input: &InvoiceInput) -> Result<InvoiceOutcome, Denial> {
    let policy = &input.policy;
    validate_invoice_policy(policy)?;
    let facts = parse_invoice(&input.document)?;
    let vendor = &input.vendor;
    let po = &input.po;

    if vendor.scope != policy.scope || po.scope != policy.scope {
        return Err(Denial::ScopeMismatch);
    }
    if vendor.category == 0
        || vendor.recipient == [0; 20]
        || vendor.valid_after > vendor.valid_until
        || po.valid_after > po.valid_until
        || po.max_total == 0
        || po.lines.len() > MAX_LINES
        || po.rate_num == 0
        || po.rate_den == 0
        || minor_units(&po.currency).is_none()
        || (po.currency == "USD" && (po.rate_num, po.rate_den) != USD_RATE)
    {
        return Err(Denial::InvalidEvidence);
    }
    // The buyer's key cannot exceed what the owner's policy lets an order authorise.
    if po.max_total > policy.max_po_total
        || !policy.currencies.contains(&po.currency)
        || po
            .lines
            .iter()
            .any(|l| !policy.po_categories.contains(&l.category))
    {
        return Err(Denial::InvalidEvidence);
    }
    verify(
        &policy.registry_key,
        &invoice_vendor_message(vendor),
        &input.vendor_signature,
    )?;
    verify(&policy.po_key, &po_message(po), &input.po_signature)?;

    let seller = tax_id_hash(&facts.seller_tax_id);
    if vendor.tax_id != seller || po.vendor_tax_id != seller {
        return Err(Denial::VendorMismatch);
    }
    if facts.po_ref.as_deref().map(reference_hash) != Some(po.po_id) {
        return Err(Denial::PoMismatch);
    }

    // This is mandatory authorization evidence, not a rule atom: an Any branch
    // cannot bypass invoice authentication. Missing evidence can be escalated to
    // the buyer, but never produces an automatic authorization.
    let attestation = match (&input.invoice_attestation, &input.invoice_signature) {
        (None, None) => {
            return Ok(InvoiceOutcome::Ask(vec![
                AskReason::InvoiceAttestationMissing,
            ]))
        }
        (Some(attestation), Some(signature)) => {
            if attestation.scope != policy.scope {
                return Err(Denial::ScopeMismatch);
            }
            if attestation.valid_after > attestation.valid_until {
                return Err(Denial::InvalidEvidence);
            }
            if attestation.customer != policy.customer
                || attestation.po_id != po.po_id
                || attestation.document_hash != facts.doc_hash
            {
                return Err(Denial::RequestMismatch);
            }
            verify(
                &policy.invoice_key,
                &invoice_attestation_message(attestation),
                signature,
            )?;
            attestation
        }
        _ => return Err(Denial::InvalidEvidence),
    };

    // Currency first: totals and amounts mean nothing in a currency the checker
    // cannot read, or at a rate the order does not give.
    let mut ask = Vec::new();
    if minor_units(&facts.currency).is_none() {
        ask.push(AskReason::UnsupportedCurrency);
    } else {
        if !facts.currency_consistent || facts.currency != po.currency {
            ask.push(AskReason::CurrencyMismatch);
        }
        if !facts.precise {
            ask.push(AskReason::CurrencyPrecision);
        }
    }
    if !ask.is_empty() {
        return Ok(InvoiceOutcome::Ask(ask));
    }
    if !facts.totals_consistent {
        ask.push(AskReason::TotalsInconsistent);
    }
    let Some(amount) = usdc_amount(&facts, po) else {
        ask.push(AskReason::PayableUnknown);
        return Ok(InvoiceOutcome::Ask(ask));
    };
    if !ask.is_empty() {
        return Ok(InvoiceOutcome::Ask(ask));
    }

    let request = Request {
        scope: policy.scope.clone(),
        task_id: obligation_id(&facts.seller_tax_id, &facts.invoice_number),
        deliverable_hash: facts.doc_hash,
        recipient: vendor.recipient,
        amount,
    };

    let mut valid_after = policy
        .valid_after
        .max(vendor.valid_after)
        .max(po.valid_after)
        .max(attestation.valid_after);
    let mut valid_until = policy
        .valid_until
        .min(vendor.valid_until)
        .min(po.valid_until)
        .min(attestation.valid_until);
    let accepted = match (
        &policy.acceptance_key,
        &input.acceptance,
        &input.acceptance_signature,
    ) {
        (_, None, None) => None,
        (Some(key), Some(acceptance), Some(signature)) => {
            if acceptance.scope != policy.scope || acceptance.valid_after > acceptance.valid_until {
                return Err(Denial::InvalidEvidence);
            }
            if acceptance.task_id != request.task_id
                || acceptance.deliverable_hash != request.deliverable_hash
                || acceptance.recipient != request.recipient
                || acceptance.amount != request.amount
            {
                return Err(Denial::RequestMismatch);
            }
            verify(key, &crate::acceptance_message(acceptance), signature)?;
            valid_after = valid_after.max(acceptance.valid_after);
            valid_until = valid_until.min(acceptance.valid_until);
            Some(acceptance.accepted)
        }
        _ => return Err(Denial::InvalidEvidence),
    };
    if valid_after > valid_until {
        return Err(Denial::EmptyValidityWindow);
    }

    let line_labels = admit_labels(&facts, &input.claims, po, &policy.lexicon)?;
    let f3 = Facts3 {
        amount,
        category: vendor.category,
        accepted,
        deliverable: facts.doc_hash,
        recipient: vendor.recipient,
        line_labels: line_labels.clone(),
        no_denied_term: Some(!denied_term_found(&facts, &policy.deny_terms)),
        po_remaining: Some(po.max_total.saturating_sub(input.po_spent)),
    };
    match decide(&policy.rule, &f3) {
        Decision::Deny => Err(Denial::PolicyDenied),
        Decision::Ask => {
            let unlabeled: Vec<u16> = (0..line_labels.len() as u16)
                .filter(|i| line_labels[usize::from(*i)].is_none())
                .collect();
            if !unlabeled.is_empty() {
                ask.push(AskReason::UnlabeledLines(unlabeled));
            }
            if accepted.is_none() && mentions_accepted(&policy.rule) {
                ask.push(AskReason::AcceptanceMissing);
            }
            if ask.is_empty() {
                ask.push(AskReason::Undetermined);
            }
            Ok(InvoiceOutcome::Ask(ask))
        }
        Decision::Allow => Ok(InvoiceOutcome::Allow(Box::new(InvoiceAuthorization {
            authorization: Authorization {
                policy_hash: invoice_policy_hash(policy),
                request,
                policy_version: policy.version,
                valid_after,
                valid_until,
                evidence_hash: hash_tagged(
                    b"warrant/invoice-evidence/v1",
                    &(
                        CHECKER_VERSION,
                        facts.doc_hash,
                        &input.claims,
                        vendor,
                        &input.vendor_signature,
                        po,
                        &input.po_signature,
                        attestation,
                        &input.invoice_signature,
                        &input.acceptance,
                        &input.acceptance_signature,
                    ),
                ),
            },
            po_id: po.po_id,
            po_max_total: po.max_total,
            customer: policy.customer,
        }))),
    }
}
