// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {Vm, JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";

contract TaskEscrowTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant AGENT = address(0x1234);
    address constant RELAYER = address(0x5678);
    bytes32 constant POLICY = keccak256("approved-policy");
    TestToken token;
    JournalVerifier verifier;
    TaskEscrow escrow;
    bytes32 id;

    function setUp() public {
        vm.warp(1000);
        token = new TestToken();
        verifier = new JournalVerifier();
        escrow = new TaskEscrow(address(token), address(verifier), bytes32(uint256(1)));
        token.mint(address(this), 1000);
        token.approve(address(escrow), 1000);
        id = escrow.offer(bytes32(uint256(1)), AGENT, POLICY, 1, 100, 1200, 2000);
    }

    function state() internal view returns (TaskEscrow.State s) {
        (,,,,,,, s) = escrow.tasks(id);
    }

    function accept() internal {
        vm.prank(AGENT);
        escrow.accept(id);
    }

    function authorization() internal view returns (PolicyExecutionVault.Authorization memory) {
        return PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(escrow),
            address(token),
            AGENT,
            100,
            id,
            keccak256("work"),
            1,
            1000,
            2000,
            keccak256("evidence")
        );
    }

    function journal() internal returns (bytes memory j) {
        j = abi.encode(authorization());
        verifier.approve(j);
    }

    function testReservesBeforeAcceptance() public {
        require(escrow.totalReserved() == 100 && token.balanceOf(address(escrow)) == 100);
        bytes memory j = journal();
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        vm.expectRevert();
        escrow.accept(id);
        accept();
        require(state() == TaskEscrow.State.Accepted);
    }

    function testAcceptedAgreementCannotBeCancelledOrReplaced() public {
        accept();
        vm.expectRevert();
        escrow.refund(id);
        vm.prank(AGENT);
        vm.expectRevert();
        escrow.refund(id);
        vm.expectRevert();
        escrow.offer(bytes32(uint256(1)), RELAYER, keccak256("replacement"), 2, 1, 1200, 3000);
        require(escrow.totalReserved() == 100 && state() == TaskEscrow.State.Accepted);
    }

    function testPermissionlessSettlementAndNoDoublePayment() public {
        accept();
        bytes memory j = journal();
        vm.prank(RELAYER);
        escrow.settle(hex"abcd", j);
        require(token.balanceOf(AGENT) == 100 && token.balanceOf(RELAYER) == 0);
        require(escrow.totalReserved() == 0 && state() == TaskEscrow.State.Paid);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        vm.warp(2001);
        vm.expectRevert();
        escrow.refund(id);
    }

    function testOfferCancellationAndTerminalId() public {
        vm.prank(RELAYER);
        vm.expectRevert();
        escrow.refund(id);
        escrow.refund(id);
        require(token.balanceOf(address(this)) == 1000 && escrow.totalReserved() == 0);
        vm.prank(AGENT);
        vm.expectRevert();
        escrow.accept(id);
        vm.expectRevert();
        escrow.offer(bytes32(uint256(1)), AGENT, POLICY, 1, 100, 1200, 2000);
    }

    function testUnacceptedOfferTimeout() public {
        vm.warp(1201);
        vm.prank(AGENT);
        vm.expectRevert();
        escrow.accept(id);
        vm.prank(RELAYER);
        escrow.refund(id);
        require(token.balanceOf(address(this)) == 1000 && token.balanceOf(RELAYER) == 0);
    }

    function testAcceptanceAndSettlementDeadlineInclusive() public {
        vm.warp(1200);
        accept();
        bytes memory j = journal();
        vm.warp(2000);
        vm.expectRevert();
        escrow.refund(id);
        escrow.settle(hex"abcd", j);
    }

    function testAcceptedTimeoutRejectsEvenValidProof() public {
        accept();
        bytes memory j = journal();
        vm.warp(2001);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        vm.prank(RELAYER);
        escrow.refund(id);
        require(state() == TaskEscrow.State.Refunded && escrow.totalReserved() == 0);
        require(token.balanceOf(address(this)) == 1000);
        vm.expectRevert();
        escrow.refund(id);
    }

    function testEveryJournalWordIsBound() public {
        accept();
        bytes memory original = journal();
        for (uint256 word; word < 12; word++) {
            bytes memory changed = abi.encode(authorization());
            changed[word * 32 + 31] = bytes1(uint8(changed[word * 32 + 31]) ^ 1);
            vm.expectRevert();
            escrow.settle(hex"abcd", changed);
        }
        require(state() == TaskEscrow.State.Accepted && escrow.totalReserved() == 100);
        escrow.settle(hex"abcd", original);
    }

    function testContractRejectsDifferentTermsEvenIfVerifierAccepts() public {
        accept();
        PolicyExecutionVault.Authorization memory a = authorization();
        a.amount = 99;
        bytes memory j = abi.encode(a);
        verifier.approve(j);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        a = authorization();
        a.policyHash = keccak256("other");
        j = abi.encode(a);
        verifier.approve(j);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        a = authorization();
        a.recipient = RELAYER;
        j = abi.encode(a);
        verifier.approve(j);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
    }

    function testTransferFailureRestoresReservation() public {
        accept();
        bytes memory j = journal();
        token.setFail(true);
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        require(state() == TaskEscrow.State.Accepted && escrow.totalReserved() == 100);
        token.setFail(false);
        escrow.settle(hex"abcd", j);
    }

    function testRefundFailureRestoresReservation() public {
        token.setFail(true);
        vm.expectRevert();
        escrow.refund(id);
        require(state() == TaskEscrow.State.Offered && escrow.totalReserved() == 100);
        token.setFail(false);
        escrow.refund(id);
    }

    function testIndependentTaskReservations() public {
        bytes32 second = escrow.offer(bytes32(uint256(2)), RELAYER, POLICY, 1, 200, 1200, 2000);
        require(escrow.totalReserved() == 300);
        escrow.refund(second);
        accept();
        escrow.settle(hex"abcd", journal());
        require(escrow.totalReserved() == 0 && token.balanceOf(AGENT) == 100);
    }

    function testUnfundedOfferRevertsAtomically() public {
        bytes32 salt = bytes32(uint256(3));
        vm.expectRevert();
        escrow.offer(salt, AGENT, POLICY, 1, 901, 1200, 2000);
        (,,,,,,, TaskEscrow.State s) = escrow.tasks(escrow.taskIdFor(address(this), salt));
        require(s == TaskEscrow.State.Missing && escrow.totalReserved() == 100);
    }

    function testInvalidTerms() public {
        vm.expectRevert();
        escrow.offer(bytes32(uint256(2)), AGENT, POLICY, 1, 100, 1200, 1200);
        vm.expectRevert();
        escrow.offer(bytes32(uint256(2)), address(0), POLICY, 1, 100, 1200, 2000);
        vm.expectRevert();
        escrow.offer(bytes32(uint256(2)), AGENT, bytes32(0), 1, 100, 1200, 2000);
    }
}
