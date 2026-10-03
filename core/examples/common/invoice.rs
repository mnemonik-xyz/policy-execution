//! Synthetic invoice fixture shared by `tests/invoice.rs` and the `invoice_fixture`
//! example. Public test keys and parties only; never use them for real payments.
#![allow(dead_code)]

use k256::ecdsa::{signature::Signer, Signature, SigningKey};
use warrant_policy::evidence::*;
use warrant_policy::*;

pub const USDC: u64 = 1_000_000;
pub const TAX_ID: &str = "US-12-3456789";

pub fn key(byte: u8) -> SigningKey {
    SigningKey::from_slice(&[byte; 32]).unwrap()
}
pub fn public(k: &SigningKey) -> Vec<u8> {
    k.verifying_key().to_encoded_point(true).as_bytes().to_vec()
}
pub fn sign(k: &SigningKey, message: &[u8]) -> Vec<u8> {
    let sig: Signature = k.sign(message);
    sig.to_bytes().to_vec()
}
pub struct Line {
    pub name: &'static str,
    pub item_id: Option<&'static str>,
    pub amount: &'static str,
}

pub struct Doc {
    pub number: &'static str,
    pub currency: &'static str,
    pub tax_id: &'static str,
    pub po_ref: &'static str,
    pub lines: Vec<Line>,
    pub line_total: &'static str,
    pub tax: &'static str,
    pub inclusive: &'static str,
    pub payable: &'static str,
    pub extra: &'static str,
    pub prolog: &'static str,
}

pub fn doc() -> Doc {
    Doc {
        number: "INV-1001",
        currency: "USD",
        tax_id: TAX_ID,
        po_ref: "PO-77",
        lines: vec![
            Line {
                name: "Annual software license",
                item_id: Some("SW-1"),
                amount: "1000.00",
            },
            Line {
                name: "Consulting services",
                item_id: Some("CS-9"),
                amount: "200.00",
            },
        ],
        line_total: "1200.00",
        tax: "120.00",
        inclusive: "1320.00",
        payable: "1320.00",
        extra: "",
        prolog: "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
    }
}

pub fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;")
}

impl Doc {
    pub fn xml(&self) -> Vec<u8> {
        let c = self.currency;
        let lines: String = self
            .lines
            .iter()
            .enumerate()
            .map(|(i, l)| {
                let id = l.item_id.map_or(String::new(), |id| {
                    format!("<cac:SellersItemIdentification><cbc:ID>{id}</cbc:ID></cac:SellersItemIdentification>")
                });
                format!(
                    "<cac:InvoiceLine><cbc:ID>{n}</cbc:ID><cbc:LineExtensionAmount currencyID=\"{c}\">{a}</cbc:LineExtensionAmount>\
                     <cac:Item><cbc:Name>{name}</cbc:Name>{id}</cac:Item></cac:InvoiceLine>",
                    n = i + 1,
                    a = l.amount,
                    name = escape(l.name),
                )
            })
            .collect();
        format!(
            "{prolog}<Invoice xmlns=\"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2\" \
             xmlns:cac=\"urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2\" \
             xmlns:cbc=\"urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2\">\
             <cbc:ID>{number}</cbc:ID><cbc:DocumentCurrencyCode>{c}</cbc:DocumentCurrencyCode>\
             <cac:OrderReference><cbc:ID>{po}</cbc:ID></cac:OrderReference>\
             <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>{tax_id}</cbc:CompanyID>\
             </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>{extra}\
             <cac:TaxTotal><cbc:TaxAmount currencyID=\"{c}\">{tax}</cbc:TaxAmount></cac:TaxTotal>\
             <cac:LegalMonetaryTotal><cbc:LineExtensionAmount currencyID=\"{c}\">{lt}</cbc:LineExtensionAmount>\
             <cbc:TaxExclusiveAmount currencyID=\"{c}\">{lt}</cbc:TaxExclusiveAmount>\
             <cbc:TaxInclusiveAmount currencyID=\"{c}\">{inc}</cbc:TaxInclusiveAmount>\
             <cbc:PayableAmount currencyID=\"{c}\">{pay}</cbc:PayableAmount></cac:LegalMonetaryTotal>\
             {lines}</Invoice>",
            prolog = self.prolog,
            number = self.number,
            po = self.po_ref,
            tax_id = self.tax_id,
            extra = self.extra,
            tax = self.tax,
            lt = self.line_total,
            inc = self.inclusive,
            pay = self.payable,
        )
        .into_bytes()
    }
}

pub fn policy(rule: Rule, scope: Scope, base: u64) -> InvoicePolicy {
    InvoicePolicy {
        version: 1,
        customer: [8; 20],
        scope,
        valid_after: base,
        valid_until: base + 4000,
        registry_key: public(&key(1)),
        po_key: public(&key(3)),
        invoice_key: public(&key(5)),
        acceptance_key: Some(public(&key(2))),
        max_po_total: 5_000 * USDC,
        po_categories: vec![7, 9],
        lexicon: vec![
            LexiconEntry {
                label: 7,
                terms: vec!["software license".into(), "subscription".into()],
            },
            LexiconEntry {
                label: 9,
                terms: vec!["consulting".into(), "advisory".into()],
            },
        ],
        deny_terms: vec!["vodka".into(), "tobacco".into()],
        rule,
    }
}

pub fn standard_rule() -> Rule {
    Rule::All(vec![
        Rule::LineLabelsWithin(vec![7, 9]),
        Rule::NoDeniedTerm,
        Rule::WithinPo,
        Rule::AmountAtMost(2_000 * USDC),
        Rule::VendorCategoryIn(vec![7]),
    ])
}

/// Synthetic E1 fixture: validity windows start at `base` and nest inside the policy's.
pub fn fixture_at(document: Vec<u8>, scope: Scope, base: u64) -> InvoiceInput {
    let vendor = InvoiceVendorCredential {
        scope: scope.clone(),
        recipient: [7; 20],
        category: 7,
        tax_id: tax_id_hash(TAX_ID),
        valid_after: base + 100,
        valid_until: base + 3900,
    };
    let po = PurchaseOrder {
        scope: scope.clone(),
        po_id: reference_hash("PO-77"),
        vendor_tax_id: tax_id_hash(TAX_ID),
        max_total: 3_000 * USDC,
        lines: vec![
            PoLine {
                item_id: reference_hash("SW-1"),
                category: 7,
            },
            PoLine {
                item_id: reference_hash("CS-9"),
                category: 9,
            },
        ],
        valid_after: base + 200,
        valid_until: base + 3800,
    };
    let mut input = InvoiceInput {
        policy: policy(standard_rule(), scope, base),
        document,
        claims: vec![
            LineClaim {
                line: 0,
                label: 7,
                evidence: ClaimEvidence::PoLine { po_line: 0 },
            },
            LineClaim {
                line: 1,
                label: 9,
                evidence: ClaimEvidence::PoLine { po_line: 1 },
            },
        ],
        vendor,
        vendor_signature: vec![],
        po,
        po_signature: vec![],
        invoice_attestation: None,
        invoice_signature: None,
        acceptance: None,
        acceptance_signature: None,
        po_spent: 0,
    };
    resign(&mut input);
    input
}

pub fn resign(input: &mut InvoiceInput) {
    input.vendor_signature = sign(&key(1), &invoice_vendor_message(&input.vendor));
    input.po_signature = sign(&key(3), &po_message(&input.po));
    attest(input);
}

/// The fixture issuer endorses these exact bytes. Tampering tests must NOT call this.
pub fn attest(input: &mut InvoiceInput) {
    let attestation = InvoiceAttestation {
        scope: input.policy.scope.clone(),
        customer: input.policy.customer,
        po_id: input.po.po_id,
        document_hash: invoice_document_hash(&input.document),
        valid_after: input.policy.valid_after,
        valid_until: input.policy.valid_until,
    };
    input.invoice_signature = Some(sign(&key(5), &invoice_attestation_message(&attestation)));
    input.invoice_attestation = Some(attestation);
}
