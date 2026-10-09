from warrant_ledger.ids import DocumentDenied, Ids


def test_vectors_agree_with_the_binary(warrant_ids, vectors, invoice_xml):
    ids = Ids(warrant_ids)
    assert ids.facts(invoice_xml) == vectors["facts"]
    o = vectors["obligation"]
    assert ids.obligation(o["sellerTaxId"], o["invoiceNumber"]) == o["hash"]
    n = vectors["obligationNormalised"]
    assert ids.obligation(n["sellerTaxId"], n["invoiceNumber"]) == n["hash"] == o["hash"]
    assert ids.reference(vectors["reference"]["text"]) == vectors["reference"]["hash"]
    assert ids.tax_id(vectors["taxId"]["text"]) == vectors["taxId"]["hash"]
    v = vectors["orderId"]
    assert ids.order_id(v["chainId"], v["escrow"], v["customer"], v["policyHash"], v["poId"]) == v["hash"]


def test_denied_document(warrant_ids, tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<Invoice/>")
    try:
        Ids(warrant_ids).facts(str(bad))
    except DocumentDenied:
        return
    raise AssertionError("expected DocumentDenied")
