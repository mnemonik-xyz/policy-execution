// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";
import {CallbackToken} from "./EscrowToken.t.sol";

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
}

contract InvoiceEscrowTest {
    InvoiceVm constant vm = InvoiceVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant VENDOR = address(0x1234);
    address constant RELAYER = address(0x5678);
    bytes32 constant POLICY = keccak256("buyer-policy");
    bytes32 constant PO = keccak256("PO-77");
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
        id = escrow.offer(POLICY, 1, PO, VENDOR, 3000, 1200, 5000);
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
        j = abi.encode(a, poId, poMax);
        verifier.approve(j);
    }

    function invoice(bytes32 taskId, uint64 amount) internal returns (bytes memory) {
        return encode(base(taskId, amount), PO, 3000);
    }

    function spent() internal view returns (uint64 s) {
        (,,,,, s,,,) = escrow.orders(id);
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
        bytes32 other = escrow.offer(keccak256("other-policy"), 1, PO, VENDOR, 500, 1200, 5000);
        vm.prank(VENDOR);
        escrow.accept(other);
        PolicyExecutionVault.Authorization memory a = base(keccak256("INV-1"), 100);
        a.policyHash = keccak256("other-policy");
        bytes memory crossOrder = encode(a, PO, 500);
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(hex"abcd", crossOrder);
    }

    function testSameOrderNumberUnderDifferentPoliciesDoesNotCollide() public {
        bytes32 other = escrow.offer(keccak256("other-buyer"), 1, PO, VENDOR, 500, 1200, 5000);
        require(other != id && escrow.totalReserved() == 3500);
        vm.expectRevert(InvoiceEscrow.InvalidState.selector);
        escrow.offer(POLICY, 1, PO, RELAYER, 1, 1200, 5000);
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
        bytes memory unproved = abi.encode(base(keccak256("INV-1"), 100), PO, uint64(3000));
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
        bytes32 second = escrow.offer(POLICY, 1, keccak256("PO-78"), VENDOR, 100, 1200, 5000);
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
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 1, PO, address(0), 1, 1200, 5000);
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 1, bytes32(0), VENDOR, 1, 1200, 5000);
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 0, PO, VENDOR, 1, 1200, 5000);
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 1, PO, VENDOR, 0, 1200, 5000);
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 1, PO, VENDOR, 1, 1200, 1200);
        vm.expectRevert(InvoiceEscrow.InvalidTerms.selector);
        escrow.offer(p, 1, PO, VENDOR, 1, 999, 5000);
    }

    function testFeeOnTransferFundingIsRejected() public {
        CallbackToken feeToken = new CallbackToken();
        InvoiceEscrow feeEscrow = new InvoiceEscrow(address(feeToken), address(verifier), bytes32(uint256(1)));
        feeToken.mint(address(this), 100);
        feeToken.approve(address(feeEscrow), 100);
        feeToken.configure(address(0), "", true);
        vm.expectRevert(InvoiceEscrow.InvalidFunding.selector);
        feeEscrow.offer(POLICY, 1, PO, VENDOR, 100, 1200, 5000);
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

    /// The Rust evidence checker's journal decodes to the same fields in Solidity.
    function testRustJournalDecodesFieldForField() public view {
        string memory json = vm.readFile("test/fixtures/invoice-journal.json");
        bytes memory journal = vm.parseJsonBytes(json, ".journal");
        require(journal.length == 14 * 32);
        (PolicyExecutionVault.Authorization memory a, bytes32 poId, uint64 poMax) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64));
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
}
