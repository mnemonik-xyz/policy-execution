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
pub const CHECKER_VERSION: u32 = 1;
const USDC_DECIMALS: u32 = 6;
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

/// Owner-approved policy for invoice payments. Immutable once a vault is deployed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvoicePolicy {
    pub version: u64,
    pub scope: Scope,
    pub valid_after: u64,
    pub valid_until: u64,
    pub registry_key: Vec<u8>,
    /// The buyer's key. It signs purchase orders and nothing else.
    pub po_key: Vec<u8>,
    pub acceptance_key: Option<Vec<u8>>,
    /// Bounds on what a purchase order may authorise.
    pub max_po_total: u64,
    pub po_categories: Vec<u16>,
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
    /// Lifetime ceiling across every invoice paid against this order.
    pub max_total: u64,
    pub lines: Vec<PoLine>,
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
    pub acceptance: Option<Acceptance>,
    pub acceptance_signature: Option<Vec<u8>>,
    /// Spend already recorded against this order, read from the vault. The vault
    /// re-checks it on chain; a stale value cannot cause an over-payment there.
    pub po_spent: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct InvoiceAuthorization {
    pub authorization: Authorization,
    pub po_id: Hash,
    pub po_max_total: u64,
}

impl InvoiceAuthorization {
    /// The 12 base journal words followed by `poId` and `poMaxTotal`.
    pub fn journal(&self) -> Vec<u8> {
        let mut out = self.authorization.journal();
        out.extend_from_slice(&self.po_id);
        out.extend_from_slice(&[0; 24]);
        out.extend_from_slice(&self.po_max_total.to_be_bytes());
        out
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AskReason {
    NotUsd,
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
    pub usd: bool,
    /// In USDC base units; `None` when missing, negative or too precise.
    pub payable: Option<u64>,
    pub totals_consistent: bool,
    pub lines: Vec<InvoiceLine>,
}

pub fn invoice_policy_hash(policy: &InvoicePolicy) -> Hash {
    hash_tagged(b"warrant/invoice-policy/v1", policy)
}

pub fn invoice_vendor_message(credential: &InvoiceVendorCredential) -> Vec<u8> {
    tagged_bytes(b"warrant/invoice-vendor/v1", credential)
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
        && valid_scope(&policy.scope)
        && policy.valid_after <= policy.valid_until
        && valid_key(&policy.registry_key)
        && valid_key(&policy.po_key)
        && policy.acceptance_key.as_deref().is_none_or(valid_key)
        && policy.max_po_total > 0
        && !policy.po_categories.is_empty()
        && policy.po_categories.len() <= 64
        && !policy.po_categories.contains(&0)
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

/// Exact decimal to base units at `USDC_DECIMALS`. Exponents, signs other than a
/// leading minus, and excess precision are not representable and yield `None`.
fn parse_amount(raw: &str) -> Option<i128> {
    let raw = raw.trim();
    let (negative, digits) = match raw.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, raw),
    };
    let (whole, frac) = digits.split_once('.').unwrap_or((digits, ""));
    if whole.is_empty()
        || whole.len() > 18
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || frac.len() > USDC_DECIMALS as usize
        || (digits.contains('.') && frac.is_empty())
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let mut value: i128 = whole.parse().ok()?;
    let mut scale = 0;
    for b in frac.bytes() {
        value = value * 10 + i128::from(b - b'0');
        scale += 1;
    }
    value *= 10i128.pow(USDC_DECIMALS - scale);
    Some(if negative { -value } else { value })
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
    let doc_hash: Hash = Sha256::digest(document).into();
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

    // Every monetary amount must carry the document currency.
    let mut usd = currency == "USD";
    let mut amount = |el: Option<&Element>| -> Option<i128> {
        let el = el?;
        if el.attr("currencyID") != Some(currency.as_str()) {
            usd = false;
        }
        parse_amount(&el.text)
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
        sum = sum.zip(line_amount).map(|(a, b)| a + b);
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
    let payable = payable_raw
        .filter(|v| *v > 0)
        .and_then(|v| u64::try_from(v).ok());

    Ok(InvoiceFacts {
        doc_hash,
        invoice_number,
        seller_tax_id,
        po_ref,
        usd,
        payable,
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
    {
        return Err(Denial::InvalidEvidence);
    }
    // The buyer's key cannot exceed what the owner's policy lets an order authorise.
    if po.max_total > policy.max_po_total
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

    let mut ask = Vec::new();
    if !facts.usd {
        ask.push(AskReason::NotUsd);
    }
    if !facts.totals_consistent {
        ask.push(AskReason::TotalsInconsistent);
    }
    let Some(amount) = facts.payable else {
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
        .max(po.valid_after);
    let mut valid_until = policy
        .valid_until
        .min(vendor.valid_until)
        .min(po.valid_until);
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
                        &input.acceptance,
                        &input.acceptance_signature,
                    ),
                ),
            },
            po_id: po.po_id,
            po_max_total: po.max_total,
        }))),
    }
}
