// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;
import {SolverBountyEscrow} from "../src/SolverBountyEscrow.sol";
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {Vm, JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";
import {CallbackToken} from "./EscrowToken.t.sol";

interface SolverLogVm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
    function readFile(string calldata path) external view returns (string memory);
    function parseJsonBytes(string calldata json, string calldata key) external pure returns (bytes memory);
}

contract SolverBountyEscrowTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    SolverLogVm constant logVm = SolverLogVm(address(vm));
    address constant SELLER = address(0x1234);
    address constant RELAYER = address(0x5678);
    bytes32 constant POLICY = keccak256("solver policy");
    CallbackToken token;
    JournalVerifier verifier;
    SolverBountyEscrow escrow;
    bytes32 id;

    function setUp() public {
        vm.warp(1000);
        token = new CallbackToken();
        verifier = new JournalVerifier();
        escrow = new SolverBountyEscrow(address(token), address(verifier), bytes32(uint256(1)));
        token.mint(address(this), 1000);
        token.approve(address(escrow), 1000);
        id = escrow.offer(bytes32(uint256(1)), SELLER, POLICY, 1, 100, 1200, 2000);
        vm.prank(SELLER);
        escrow.accept(id);
    }

    function result() internal pure returns (bytes memory) {
        return abi.encodePacked("warrant/schedule/v1", uint32(1), uint32(0), uint32(1), uint64(4));
    }

    function authorization() internal view returns (PolicyExecutionVault.Authorization memory) {
        return PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(escrow),
            address(token),
            SELLER,
            100,
            id,
            sha256(result()),
            1,
            1000,
            2000,
            keccak256("solver evidence")
        );
    }

    function journal() internal returns (bytes memory j) {
        j = abi.encode(authorization(), uint64(14));
        verifier.approve(j); // Mock verifier: these tests cover settlement, not schedule correctness.
    }

    function state() internal view returns (TaskEscrow.State s) {
        (,,,,,,, s) = escrow.tasks(id);
    }

    function testBuyerOfflineRelayerPublishesResultAndPaysOnlySeller() public {
        bytes memory j = journal();
        logVm.recordLogs();
        vm.prank(RELAYER);
        escrow.settleWithResult(hex"abcd", j, result());
        require(token.balanceOf(SELLER) == 100 && token.balanceOf(RELAYER) == 0);
        require(state() == TaskEscrow.State.Paid && escrow.totalReserved() == 0);
        SolverLogVm.Log[] memory logs = logVm.getRecordedLogs();
        bool found;
        for (uint256 i; i < logs.length; i++) {
            if (
                logs[i].emitter == address(escrow)
                    && logs[i].topics[0] == keccak256("ResultPublished(bytes32,bytes32,uint64,bytes)")
            ) {
                require(logs[i].topics[1] == id && logs[i].topics[2] == sha256(result()));
                (uint64 cost, bytes memory published) = abi.decode(logs[i].data, (uint64, bytes));
                require(cost == 14 && keccak256(published) == keccak256(result()));
                found = true;
            }
        }
        require(found, "result not published");
    }

    function testInheritedHashOnlyPathAlwaysReverts() public {
        bytes memory j = journal();
        vm.expectRevert();
        escrow.settle(hex"abcd", j);
        bytes memory legacy = abi.encode(authorization());
        vm.expectRevert();
        TaskEscrow(address(escrow)).settle(hex"abcd", legacy);
        require(state() == TaskEscrow.State.Accepted);
    }

    function testWrongMissingOversizedOrUnprovedResultCannotPay() public {
        bytes memory j = journal();
        bytes memory altered = result();
        altered[37] ^= 0x01;
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, altered);
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, hex"");
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, new bytes(2072));
        vm.expectRevert();
        escrow.settleWithResult(hex"abce", j, result());
        require(state() == TaskEscrow.State.Accepted && escrow.totalReserved() == 100);
    }

    function testEveryJournalWordIncludingComputedCostIsBound() public {
        bytes memory j = journal();
        for (uint256 word; word < 13; word++) {
            j[word * 32 + 31] ^= 0x01;
            vm.expectRevert();
            escrow.settleWithResult(hex"abcd", j, result());
            j[word * 32 + 31] ^= 0x01;
        }
        escrow.settleWithResult(hex"abcd", j, result());
    }

    function testLiveTermsStillRejectAnApprovedWrongRecipient() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        a.recipient = RELAYER;
        bytes memory j = abi.encode(a, uint64(14));
        verifier.approve(j);
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, result());
        a = authorization();
        a.amount = 101;
        j = abi.encode(a, uint64(14));
        verifier.approve(j);
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, result());
    }

    function testBuyerCannotCancelAndSellerCanSettleAtDeadline() public {
        bytes memory j = journal();
        vm.expectRevert();
        escrow.refund(id);
        vm.warp(2000);
        vm.expectRevert();
        escrow.refund(id);
        escrow.settleWithResult(hex"abcd", j, result());
    }

    function testUnfinishedTaskRefundsAndLateProofCannotPay() public {
        bytes memory j = journal();
        vm.warp(2001);
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, result());
        vm.prank(RELAYER);
        escrow.refund(id);
        require(token.balanceOf(address(this)) == 1000 && escrow.totalReserved() == 0);
    }

    function testReplayAndLegacyJournalsRejected() public {
        bytes memory j = journal();
        bytes memory legacy = abi.encode(authorization());
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", legacy, result());
        escrow.settleWithResult(hex"abcd", j, result());
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, result());
    }

    function testTransferFailureRollsBackPaymentAndPublication() public {
        bytes memory j = journal();
        token.setFail(true);
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, result());
        require(state() == TaskEscrow.State.Accepted && escrow.totalReserved() == 100);
        token.setFail(false);
        escrow.settleWithResult(hex"abcd", j, result());
    }

    function testTokenCannotReenterResultSettlement() public {
        bytes memory j = journal();
        token.configure(address(escrow), abi.encodeCall(escrow.settleWithResult, (hex"abcd", j, result())), false);
        escrow.settleWithResult(hex"abcd", j, result());
        require(!token.callbackSucceeded() && token.balanceOf(SELLER) == 100 && escrow.totalReserved() == 0);
    }

    function testFuzzAnyMutatedResultByteFails(uint16 position, bytes1 delta) public {
        if (delta == 0) delta = 0x01;
        bytes memory altered = result();
        altered[uint256(position) % altered.length] ^= delta;
        bytes memory j = journal();
        vm.expectRevert();
        escrow.settleWithResult(hex"abcd", j, altered);
        require(state() == TaskEscrow.State.Accepted);
    }

    function testRustJournalAndCanonicalScheduleDecodeExactly() public view {
        string memory json = logVm.readFile("test/fixtures/solver-journal.json");
        bytes memory j = logVm.parseJsonBytes(json, ".journal");
        bytes memory r = logVm.parseJsonBytes(json, ".result");
        require(j.length == 416);
        (PolicyExecutionVault.Authorization memory a, uint64 cost) =
            abi.decode(j, (PolicyExecutionVault.Authorization, uint64));
        bytes memory expected = abi.encodePacked(
            "warrant/schedule/v1",
            uint32(3),
            uint32(0),
            uint32(1),
            uint64(0),
            uint32(1),
            uint32(1),
            uint64(0),
            uint32(2),
            uint32(1),
            uint64(4)
        );
        require(keccak256(expected) == keccak256(r) && sha256(r) == a.deliverableHash);
        require(cost == 14 && a.amount == 10_000_000 && a.chainId == 31337);
        require(a.validAfter == 1000 && a.validUntil == 5000 && a.policyVersion == 1);
        require(a.recipient == address(0x0505050505050505050505050505050505050505));
    }
}
