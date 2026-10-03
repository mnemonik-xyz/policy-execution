// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;
import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";
import {CallbackToken} from "./EscrowToken.t.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

interface InvoiceVm {
    function prank(address) external;
    function warp(uint256) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function readFile(string calldata) external view returns (string memory);
    function parseJsonBytes(string calldata, string calldata) external pure returns (bytes memory);
    function parseJsonBytes32(string calldata, string calldata) external pure returns (bytes32);
    function parseJsonUint(string calldata, string calldata) external pure returns (uint256);
    function parseJsonAddress(string calldata, string calldata) external pure returns (address);
    function assume(bool) external pure;
    function sign(uint256, bytes32) external pure returns (uint8, bytes32, bytes32);
    function addr(uint256) external pure returns (address);
}

contract InvoiceEscrowTest {
    InvoiceVm constant vm = InvoiceVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant VENDOR = address(0x1234);
    address constant RELAYER = address(0x5678);
    bytes32 constant POLICY = keccak256("buyer-policy");
    bytes32 constant PO = keccak256("PO-77");
    uint256 constant SIGNER_KEY = 0xA11CE;
    uint64 constant THRESHOLD = 1000;
    TestToken token;
    JournalVerifier verifier;
    InvoiceEscrow escrow;
    bytes32 id;

    function setUp() public {
        vm.warp(1000);
        token = new TestToken();
        verifier = new JournalVerifier();
        escrow = new InvoiceEscrow(address(token), address(verifier), bytes32(uint256(1)));
        token.mint(address(this), 10_000);
        token.approve(address(escrow), 10_000);
        id = escrow.offer(terms(POLICY, PO, VENDOR, 3000, vm.addr(SIGNER_KEY), 1500, THRESHOLD));
    }

    /// Proofs-only terms: no signer, no allowance, no threshold.
    function terms(bytes32 policy, bytes32 po, address recipient, uint64 maxTotal)
        internal
        pure
        returns (InvoiceEscrow.Terms memory)
    {
        return terms(policy, po, recipient, maxTotal, address(0), 0, 0);
    }

    function terms(
        bytes32 policy,
        bytes32 po,
        address recipient,
        uint64 maxTotal,
        address signer,
        uint64 allowance,
        uint64 threshold
    ) internal pure returns (InvoiceEscrow.Terms memory) {
        return InvoiceEscrow.Terms(policy, 1, po, recipient, maxTotal, signer, allowance, threshold, 1200, 5000);
    }

    function accept() internal {
        vm.prank(VENDOR);
        escrow.accept(id);
    }

    function base(bytes32 taskId, uint64 amount) internal view returns (PolicyExecutionVault.Authorization memory) {
        return PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(escrow),
            address(token),
            VENDOR,
            amount,
            taskId,
            keccak256("invoice document"),
            1,
            1000,
            5000,
            keccak256("evidence")
        );
    }

    function encode(PolicyExecutionVault.Authorization memory a, bytes32 poId, uint64 poMax)
        internal
        returns (bytes memory j)
    {
        j = abi.encode(a, poId, poMax, address(this));
        verifier.approve(j);
    }

    function invoice(bytes32 taskId, uint64 amount) internal returns (bytes memory) {
        return encode(base(taskId, amount), PO, 3000);
    }

    function spent() internal view returns (uint64) {
        return escrow.order(id).spent;
    }

    function signerSpent() internal view returns (uint64) {
        return escrow.order(id).signerSpent;
    }

    /// A journal signed by the escrow's signer; the mock verifier never sees it.
    function signed(bytes32 taskId, uint64 amount) internal view returns (bytes memory j, bytes memory sig) {
        j = abi.encode(base(taskId, amount), PO, uint64(3000), address(this));
        sig = signWith(SIGNER_KEY, j);
    }

    function signWith(uint256 key, bytes memory j) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, escrow.signerDigest(j));
        return abi.encodePacked(r, s, v);
    }

    function testReservesFullOrderAndRequiresVendorAcceptance() public {
        require(escrow.totalReserved() == 3000 && token.balanceOf(address(escrow)) == 3000);
        bytes memory j = invoice(keccak256("INV-1"), 1320);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settle(hex"abcd", j);
        vm.prank(RELAYER);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.accept(id);
        accept();
        vm.prank(RELAYER);
        escrow.settle(hex"abcd", j);
        require(token.balanceOf(VENDOR) == 1320 && spent() == 1320 && escrow.totalReserved() == 1680);
    }

    function testManyInvoicesWithinCeilingThenExceeded() public {
        accept();
        escrow.settle(hex"abcd", invoice(keccak256("INV-1"), 1320));
        escrow.settle(hex"abcd", invoice(keccak256("INV-2"), 1000));
        require(escrow.remaining(id) == 680);
        bytes memory over = invoice(keccak256("INV-3"), 681);
        vm.expectRevert(InvoiceEscrow.OrderExceeded.selector);
        escrow.settle(hex"abcd", over);
        escrow.settle(hex"abcd", invoice(keccak256("INV-3"), 680));
        require(token.balanceOf(VENDOR) == 3000 && escrow.remaining(id) == 0 && escrow.totalReserved() == 0);
    }

    function testInvoicePaysOnceEvenWithAnotherValidProof() public {
        accept();
        escrow.settle(hex"abcd", invoice(keccak256("INV-1"), 100));
        bytes memory again = invoice(keccak256("INV-1"), 100);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(hex"abcd", again);
        // A second order under another policy still cannot pay the same obligation.
        bytes32 other = escrow.offer(terms(keccak256("other-policy"), PO, VENDOR, 500));
        vm.prank(VENDOR);
        escrow.accept(other);
        PolicyExecutionVault.Authorization memory a = base(keccak256("INV-1"), 100);
        a.policyHash = keccak256("other-policy");
        bytes memory crossOrder = encode(a, PO, 500);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(hex"abcd", crossOrder);
    }

    function testSameOrderNumberUnderDifferentPoliciesDoesNotCollide() public {
        bytes32 other = escrow.offer(terms(keccak256("other-buyer"), PO, VENDOR, 500));
        require(other != id && escrow.totalReserved() == 3500);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.offer(terms(POLICY, PO, RELAYER, 1));
    }

    function testProvenOrderCeilingMustMatchFundedOrder() public {
        accept();
        bytes memory j = encode(base(keccak256("INV-1"), 100), PO, 2999);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settle(hex"abcd", j);
    }

    function testEveryBoundFieldIsChecked() public {
        accept();
        for (uint256 field; field < 9; field++) {
            PolicyExecutionVault.Authorization memory a = base(keccak256(abi.encode("INV", field)), 100);
            bytes32 poId = PO;
            if (field == 0) a.policyVersion = 2;
            if (field == 1) a.chainId += 1;
            if (field == 2) a.vault = RELAYER;
            if (field == 3) a.token = RELAYER;
            if (field == 4) a.recipient = RELAYER;
            if (field == 5) a.amount = 0;
            if (field == 6) a.deliverableHash = bytes32(0);
            if (field == 7) a.evidenceHash = bytes32(0);
            if (field == 8) a.validUntil = 999;
            bytes memory j = encode(a, poId, 3000);
            vm.expectRevert();
            escrow.settle(hex"abcd", j);
        }
        // An order ID that was never funded finds no accepted order.
        bytes memory missing = encode(base(keccak256("INV-X"), 100), keccak256("PO-00"), 3000);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settle(hex"abcd", missing);
        require(spent() == 0);
    }

    function testUnprovedOrMalformedJournalsAreRejected() public {
        accept();
        bytes memory unproved = abi.encode(base(keccak256("INV-1"), 100), PO, uint64(3000), address(this));
        vm.expectRevert();
        escrow.settle(hex"abcd", unproved);
        bytes memory taskFormat = abi.encode(base(keccak256("INV-1"), 100));
        verifier.approve(taskFormat);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settle(hex"abcd", taskFormat);
        bytes memory j = invoice(keccak256("INV-1"), 100);
        vm.expectRevert();
        escrow.settle(hex"beef", j);
    }

    function testAcceptedOrderClosesOnlyAfterDeadlineAndRefundsRemainder() public {
        accept();
        escrow.settle(hex"abcd", invoice(keccak256("INV-1"), 1320));
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.close(id);
        vm.warp(5000);
        bytes memory late = invoice(keccak256("INV-2"), 100);
        escrow.settle(hex"abcd", late);
        vm.warp(5001);
        bytes memory expired = invoice(keccak256("INV-3"), 100);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settle(hex"abcd", expired);
        uint256 before = token.balanceOf(address(this));
        vm.prank(RELAYER);
        escrow.close(id);
        require(token.balanceOf(address(this)) == before + 1580 && escrow.totalReserved() == 0);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.close(id);
    }

    function testUnacceptedOrderCancellation() public {
        vm.prank(RELAYER);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.close(id);
        escrow.close(id);
        require(token.balanceOf(address(this)) == 10_000 && escrow.totalReserved() == 0);
        vm.prank(VENDOR);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.accept(id);
        // After the acceptance deadline anyone may return an unaccepted order.
        bytes32 second = escrow.offer(terms(POLICY, keccak256("PO-78"), VENDOR, 100));
        vm.warp(1201);
        vm.prank(VENDOR);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.accept(second);
        vm.prank(RELAYER);
        escrow.close(second);
        require(token.balanceOf(address(this)) == 10_000);
    }

    function testInvalidOfferTerms() public {
        bytes32 p = keccak256("new");
        InvoiceEscrow.Terms memory t = terms(p, PO, VENDOR, 1);
        InvoiceEscrow.Terms memory bad;
        for (uint256 field; field < 6; field++) {
            bad = t;
            if (field == 0) bad.recipient = address(0);
            if (field == 1) bad.poId = bytes32(0);
            if (field == 2) bad.policyVersion = 0;
            if (field == 3) bad.maxTotal = 0;
            if (field == 4) bad.settleBy = 1200;
            if (field == 5) bad.acceptBy = 999;
            vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
            escrow.offer(bad);
        }
    }

    function testSignerTermsMustBeConsistent() public {
        bytes32 p = keccak256("signer-terms");
        address signer = vm.addr(SIGNER_KEY);
        // A signer needs both room to act; room needs a signer; the vendor cannot sign its own invoices.
        InvoiceEscrow.Terms[5] memory bad = [
            terms(p, PO, VENDOR, 100, signer, 0, 10),
            terms(p, PO, VENDOR, 100, signer, 10, 0),
            terms(p, PO, VENDOR, 100, address(0), 10, 10),
            terms(p, PO, VENDOR, 100, address(0), 0, 10),
            terms(p, PO, VENDOR, 100, VENDOR, 10, 10)
        ];
        for (uint256 i; i < bad.length; i++) {
            vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
            escrow.offer(bad[i]);
        }
        // An allowance above the ceiling is not a valid order.
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(terms(p, PO, VENDOR, 100, signer, 101, 10));
        escrow.offer(terms(p, PO, VENDOR, 100, signer, 100, 10));
    }

    function testFeeOnTransferFundingIsRejected() public {
        CallbackToken feeToken = new CallbackToken();
        InvoiceEscrow feeEscrow = new InvoiceEscrow(address(feeToken), address(verifier), bytes32(uint256(1)));
        feeToken.mint(address(this), 100);
        feeToken.approve(address(feeEscrow), 100);
        feeToken.configure(address(0), "", true);
        vm.expectRevert(InvoiceEscrow.InvalidFunding.selector);
        feeEscrow.offer(terms(POLICY, PO, VENDOR, 100));
    }

    /// Cumulative payments never exceed the ceiling, whatever the invoice amounts.
    function testFuzzCeilingHolds(uint64 a, uint64 b) public {
        vm.assume(a > 0 && b > 0);
        accept();
        uint256 paid;
        uint64[2] memory amounts = [a, b];
        for (uint256 i; i < 2; i++) {
            bytes memory j = invoice(keccak256(abi.encode("FUZZ", i)), amounts[i]);
            if (uint256(amounts[i]) + paid <= 3000) {
                escrow.settle(hex"abcd", j);
                paid += amounts[i];
            } else {
                vm.expectRevert(InvoiceEscrow.OrderExceeded.selector);
                escrow.settle(hex"abcd", j);
            }
        }
        require(spent() == paid && token.balanceOf(VENDOR) == paid);
        require(escrow.totalReserved() == 3000 - paid && token.balanceOf(address(escrow)) == 3000 - paid);
    }

    function testSignedSettlementBelowThreshold() public {
        accept();
        (bytes memory j, bytes memory sig) = signed(keccak256("INV-S1"), 999);
        vm.prank(RELAYER);
        escrow.settleSigned(j, sig);
        require(token.balanceOf(VENDOR) == 999 && spent() == 999 && signerSpent() == 999);
        require(escrow.remaining(id) == 2001 && escrow.totalReserved() == 2001);
        // The same obligation cannot be paid again by proof either.
        bytes memory proven = invoice(keccak256("INV-S1"), 999);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(hex"abcd", proven);
    }

    function testAmountAtOrAboveThresholdRequiresProof() public {
        accept();
        (bytes memory j, bytes memory sig) = signed(keccak256("INV-S2"), THRESHOLD);
        vm.expectRevert(InvoiceEscrow.ProofRequired.selector);
        escrow.settleSigned(j, sig);
        // The proof path takes the same journal.
        verifier.approve(j);
        escrow.settle(hex"abcd", j);
        require(token.balanceOf(VENDOR) == THRESHOLD && signerSpent() == 0);
    }

    function testSignerAllowanceCapsSplitPayments() public {
        accept();
        (bytes memory j1, bytes memory s1) = signed(keccak256("INV-S3"), 900);
        escrow.settleSigned(j1, s1);
        (bytes memory j2, bytes memory s2) = signed(keccak256("INV-S4"), 601);
        vm.expectRevert(InvoiceEscrow.SignerAllowanceExceeded.selector);
        escrow.settleSigned(j2, s2);
        (bytes memory j3, bytes memory s3) = signed(keccak256("INV-S5"), 600);
        escrow.settleSigned(j3, s3);
        require(signerSpent() == 1500 && spent() == 1500);
        // Proofs are not limited by the signer allowance, only by the ceiling.
        escrow.settle(hex"abcd", invoice(keccak256("INV-S6"), 1500));
        require(spent() == 3000 && escrow.remaining(id) == 0);
    }

    function testWrongSignerAndMalformedSignaturesAreRejected() public {
        accept();
        bytes memory j = abi.encode(base(keccak256("INV-S7"), 100), PO, uint64(3000), address(this));
        bytes memory other = signWith(0xB0B, j);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(j, other);
        bytes memory sig = signWith(SIGNER_KEY, j);
        // A signature over a different journal does not transfer.
        bytes memory altered = abi.encode(base(keccak256("INV-S8"), 100), PO, uint64(3000), address(this));
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(altered, sig);
        // High-s form of a valid signature is rejected by ECDSA.recover.
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(SIGNER_KEY, escrow.signerDigest(j));
        bytes32 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes memory highS = abi.encodePacked(r, bytes32(uint256(n) - uint256(s)), v == 27 ? uint8(28) : uint8(27));
        vm.expectRevert();
        escrow.settleSigned(j, highS);
        vm.expectRevert();
        escrow.settleSigned(j, hex"1234");
        escrow.settleSigned(j, sig);
        require(token.balanceOf(VENDOR) == 100);
    }

    function testSignedPathHonoursSharedChecks() public {
        accept();
        PolicyExecutionVault.Authorization memory a = base(keccak256("INV-S9"), 100);
        a.recipient = RELAYER;
        bytes memory redirected = abi.encode(a, PO, uint64(3000), address(this));
        bytes memory sig = signWith(SIGNER_KEY, redirected);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settleSigned(redirected, sig);
        vm.warp(5001);
        (bytes memory late, bytes memory lateSig) = signed(keccak256("INV-S10"), 100);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settleSigned(late, lateSig);
    }

    function testCustomerCanRevokeSignerButNotProofs() public {
        accept();
        vm.prank(RELAYER);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.revokeSigner(id);
        escrow.revokeSigner(id);
        (bytes memory j, bytes memory sig) = signed(keccak256("INV-S11"), 100);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(j, sig);
        verifier.approve(j);
        escrow.settle(hex"abcd", j);
        require(token.balanceOf(VENDOR) == 100);
    }

    function testProofsOnlyOrderRejectsEverySignature() public {
        bytes32 p = keccak256("proofs-only");
        bytes32 o = escrow.offer(terms(p, PO, VENDOR, 100));
        vm.prank(VENDOR);
        escrow.accept(o);
        PolicyExecutionVault.Authorization memory a = base(keccak256("INV-S12"), 50);
        a.policyHash = p;
        bytes memory j = abi.encode(a, PO, uint64(100), address(this));
        bytes memory sig = signWith(SIGNER_KEY, j);
        vm.expectRevert(InvoiceEscrow.ProofRequired.selector);
        escrow.settleSigned(j, sig);
        verifier.approve(j);
        escrow.settle(hex"abcd", j);
        require(token.balanceOf(VENDOR) == 50);
    }

    /// Each order names its own signer; a key valid for one order signs nothing for another.
    function testSignerIsPerOrder() public {
        bytes32 p = keccak256("other-signer");
        uint256 otherKey = 0xB0B;
        bytes32 o = escrow.offer(terms(p, PO, VENDOR, 1000, vm.addr(otherKey), 1000, THRESHOLD));
        vm.prank(VENDOR);
        escrow.accept(o);
        accept();
        PolicyExecutionVault.Authorization memory a = base(keccak256("INV-P1"), 100);
        a.policyHash = p;
        bytes memory j = abi.encode(a, PO, uint64(1000), address(this));
        // The first order's signer cannot settle the second order.
        bytes memory wrong = signWith(SIGNER_KEY, j);
        bytes memory right = signWith(otherKey, j);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(j, wrong);
        escrow.settleSigned(j, right);
        // And the second order's signer cannot settle the first.
        (bytes memory j1, bytes memory s1) = signed(keccak256("INV-P2"), 100);
        bytes memory wrong1 = signWith(otherKey, j1);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(j1, wrong1);
        escrow.settleSigned(j1, s1);
        require(token.balanceOf(VENDOR) == 200);
        // Revoking one order leaves the other untouched.
        escrow.revokeSigner(o);
        (bytes memory j2, bytes memory s2) = signed(keccak256("INV-P3"), 100);
        escrow.settleSigned(j2, s2);
        require(token.balanceOf(VENDOR) == 300);
    }

    /// The buyer settles an undecided invoice on their own authority, within the same bounds.
    function testBuyerApprovalWithinBounds() public {
        bytes32 obligation = keccak256("INV-ASK-1");
        bytes32 document = keccak256("ask document");
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settleApproved(id, obligation, 700, document);
        accept();
        vm.prank(RELAYER);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleApproved(id, obligation, 700, document);
        vm.prank(VENDOR);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleApproved(id, obligation, 700, document);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settleApproved(id, obligation, 0, document);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settleApproved(id, bytes32(0), 700, document);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settleApproved(id, obligation, 700, bytes32(0));
        vm.expectRevert(InvoiceEscrow.OrderExceeded.selector);
        escrow.settleApproved(id, obligation, 3001, document);
        escrow.settleApproved(id, obligation, 700, document);
        require(token.balanceOf(VENDOR) == 700 && spent() == 700 && signerSpent() == 0);
        require(escrow.remaining(id) == 2300 && escrow.totalReserved() == 2300);
        // Once approved, neither a proof nor a signature nor a second approval pays it again.
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settleApproved(id, obligation, 1, document);
        bytes memory proven = invoice(obligation, 700);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(hex"abcd", proven);
        (bytes memory j, bytes memory sig) = signed(obligation, 700);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settleSigned(j, sig);
        // And an invoice paid by proof cannot be approved again.
        escrow.settle(hex"abcd", invoice(keccak256("INV-2"), 100));
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settleApproved(id, keccak256("INV-2"), 100, document);
        // Approval ignores the signer allowance and threshold but not the deadline.
        escrow.settleApproved(id, keccak256("INV-ASK-2"), 2000, document);
        require(spent() == 2800 && signerSpent() == 0);
        vm.warp(5001);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.settleApproved(id, keccak256("INV-ASK-3"), 1, document);
    }

    /// Signature-authorized payments never exceed the allowance; proofs still respect the ceiling.
    function testFuzzSignerAllowanceHolds(uint64 a, uint64 b) public {
        vm.assume(a > 0 && b > 0 && a < THRESHOLD && b < THRESHOLD);
        accept();
        uint256 paid;
        uint64[2] memory amounts = [a, b];
        for (uint256 i; i < 2; i++) {
            (bytes memory j, bytes memory sig) = signed(keccak256(abi.encode("SFUZZ", i)), amounts[i]);
            if (uint256(amounts[i]) + paid <= 1500) {
                escrow.settleSigned(j, sig);
                paid += amounts[i];
            } else {
                vm.expectRevert(InvoiceEscrow.SignerAllowanceExceeded.selector);
                escrow.settleSigned(j, sig);
            }
        }
        require(signerSpent() == paid && spent() == paid && token.balanceOf(VENDOR) == paid);
    }

    /// The Rust signer helper produces signatures this contract recovers to the expected address.
    function testRustSignatureRecoversToSigner() public view {
        string memory json = vm.readFile("test/fixtures/invoice-signature.json");
        bytes memory journal = vm.parseJsonBytes(json, ".journal");
        bytes memory signature = vm.parseJsonBytes(json, ".signature");
        bytes memory publicKey = vm.parseJsonBytes(json, ".signerPublicKey");
        require(publicKey.length == 64 && signature.length == 65);
        address expected = address(uint160(uint256(keccak256(publicKey))));
        // The fixture was signed for chain 5042002 and escrow 0x0303…03; recompute that digest.
        bytes32 digest = sha256(
            abi.encodePacked(
                bytes("warrant/invoice-signer/v1"),
                uint256(5042002),
                address(0x0303030303030303030303030303030303030303),
                sha256(journal)
            )
        );
        require(ECDSA.recover(digest, signature) == expected);
        require(signature[64] == 0x1b || signature[64] == 0x1c);
    }

    /// The Rust evidence checker's journal decodes to the same fields in Solidity.
    function testRustJournalDecodesFieldForField() public view {
        string memory json = vm.readFile("test/fixtures/invoice-journal.json");
        bytes memory journal = vm.parseJsonBytes(json, ".journal");
        require(journal.length == 15 * 32);
        (PolicyExecutionVault.Authorization memory a, bytes32 poId, uint64 poMax, address customer) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64, address));
        require(customer == vm.parseJsonAddress(json, ".customer"));
        require(a.policyHash == vm.parseJsonBytes32(json, ".policyHash"));
        require(a.chainId == vm.parseJsonUint(json, ".chainId"));
        require(a.vault == vm.parseJsonAddress(json, ".vault"));
        require(a.token == vm.parseJsonAddress(json, ".token"));
        require(a.recipient == vm.parseJsonAddress(json, ".recipient"));
        require(a.amount == vm.parseJsonUint(json, ".amount"));
        require(a.taskId == vm.parseJsonBytes32(json, ".taskId"));
        require(a.deliverableHash == vm.parseJsonBytes32(json, ".deliverableHash"));
        require(a.policyVersion == vm.parseJsonUint(json, ".policyVersion"));
        require(a.validAfter == vm.parseJsonUint(json, ".validAfter"));
        require(a.validUntil == vm.parseJsonUint(json, ".validUntil"));
        require(a.evidenceHash == vm.parseJsonBytes32(json, ".evidenceHash"));
        require(poId == vm.parseJsonBytes32(json, ".poId"));
        require(poMax == vm.parseJsonUint(json, ".poMaxTotal"));
    }

    function testCustomerCannotBeChangedBehindProofOrSignature() public {
        accept();
        address otherBuyer = address(0xB0B);
        token.mint(otherBuyer, 3000);
        vm.prank(otherBuyer);
        token.approve(address(escrow), 3000);
        vm.prank(otherBuyer);
        bytes32 otherOrder = escrow.offer(terms(POLICY, PO, VENDOR, 3000, vm.addr(SIGNER_KEY), 1500, THRESHOLD));
        vm.prank(VENDOR);
        escrow.accept(otherOrder);

        bytes32 obligation = keccak256("customer-bound-invoice");
        (bytes memory journal, bytes memory sig) = signed(obligation, 100);
        verifier.approve(journal);
        bytes memory redirected = abi.encode(base(obligation, 100), PO, uint64(3000), otherBuyer);
        vm.expectRevert();
        escrow.settle(hex"abcd", redirected);
        vm.expectRevert(InvoiceEscrow.Unauthorized.selector);
        escrow.settleSigned(redirected, sig);

        escrow.settleSigned(journal, sig);
        require(escrow.consumed(address(this), obligation));
        require(!escrow.consumed(otherBuyer, obligation));
        require(escrow.order(otherOrder).spent == 0);
        // An independently authenticated journal can pay the other customer's order.
        escrow.settleSigned(redirected, signWith(SIGNER_KEY, redirected));
        require(escrow.consumed(otherBuyer, obligation));
        require(escrow.order(otherOrder).spent == 100);
    }

    function testLegacyInvoiceJournalCannotSettle() public {
        accept();
        bytes memory legacy = abi.encode(base(keccak256("legacy"), 100), PO, uint64(3000));
        verifier.approve(legacy);
        bytes memory signature = signWith(SIGNER_KEY, legacy);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settle(hex"abcd", legacy);
        vm.expectRevert(InvoiceEscrow.InvalidAuthorization.selector);
        escrow.settleSigned(legacy, signature);
    }

    function testOtherBuyerCannotConsumeVictimObligation() public {
        accept();
        bytes32 obligation = keccak256("victim invoice");
        bytes memory victimJournal = invoice(obligation, 1320);
        address attacker = address(0xBAD);
        token.mint(attacker, 1);
        vm.prank(attacker);
        token.approve(address(escrow), 1);
        vm.prank(attacker);
        bytes32 attackOrder = escrow.offer(terms(keccak256("attacker policy"), keccak256("attacker PO"), attacker, 1));
        vm.prank(attacker);
        escrow.accept(attackOrder);
        vm.prank(attacker);
        escrow.settleApproved(attackOrder, obligation, 1, keccak256("anything"));
        require(token.balanceOf(attacker) == 1, "attacker gets own funding back");
        escrow.settle(hex"abcd", victimJournal);
    }

    function testSquatterCannotReserveAnotherCustomersOrder() public {
        address attacker = address(0xBAD);
        bytes32 victimPolicy = keccak256("upcoming victim policy");
        bytes32 victimPo = keccak256("upcoming victim PO");
        token.mint(attacker, 1);
        vm.prank(attacker);
        token.approve(address(escrow), 1);
        vm.prank(attacker);
        bytes32 attackOrder = escrow.offer(terms(victimPolicy, victimPo, attacker, 1));
        vm.prank(attacker);
        escrow.close(attackOrder);
        require(token.balanceOf(attacker) == 1, "funding refunded");
        escrow.offer(terms(victimPolicy, victimPo, VENDOR, 3000));
    }
}
