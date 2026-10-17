// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {ReceiptClaimVerifier} from "../src/ReceiptClaimVerifier.sol";
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {TestToken} from "./PolicyExecutionVault.t.sol";
import {ReceiptClaimLib} from "../vendor/risc0/contracts/src/IRiscZeroVerifier.sol";

interface ClaimVm {
    function addr(uint256 privateKey) external pure returns (address);
    function sign(uint256 privateKey, bytes32 digest) external pure returns (uint8 v, bytes32 r, bytes32 s);
    function prank(address) external;
    function warp(uint256) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function expectRevert(bytes calldata) external;
}

contract ReceiptClaimVerifierTest {
    ClaimVm constant vm = ClaimVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 constant PROVER_KEY = 0xA11CE;
    uint256 constant OTHER_KEY = 0xB0B;
    address constant AGENT = address(0x1234);
    address constant RELAYER = address(0x5678);
    bytes32 constant POLICY = keccak256("approved-policy");
    bytes32 constant IMAGE_ID = 0xafd4ad2d38c10243e72193f92a5d1ab23f3d2135f32282a9c1e43bf734dc105e;

    TestToken token;
    ReceiptClaimVerifier verifier;
    TaskEscrow escrow;
    address prover;
    bytes32 taskId;

    function setUp() public {
        vm.warp(1000);
        prover = vm.addr(PROVER_KEY);
        token = new TestToken();
        verifier = new ReceiptClaimVerifier(prover, ReceiptClaimLib.SYSTEM_STATE_ZERO_DIGEST);
        escrow = new TaskEscrow(address(token), address(verifier), IMAGE_ID);
        token.mint(address(this), 1000);
        token.approve(address(escrow), 1000);
        taskId = escrow.offer(bytes32(uint256(1)), AGENT, POLICY, 1, 100, 1200, 2000);
        vm.prank(AGENT);
        escrow.accept(taskId);
    }

    function authorization() internal view returns (PolicyExecutionVault.Authorization memory) {
        return PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(escrow),
            address(token),
            AGENT,
            100,
            taskId,
            keccak256("work"),
            1,
            1000,
            2000,
            keccak256("evidence")
        );
    }

    function buildSeal(uint256 signerKey, bytes32 imageId, bytes32 journalDigest) internal view returns (bytes memory) {
        bytes32 claim = verifier.claimDigest(imageId, journalDigest);
        bytes32 digest = verifier.receiptDigest(imageId, journalDigest);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);
        return abi.encodePacked(verifier.SELECTOR(), claim, r, s, v);
    }

    function testFuzzClaimDigestMatchesUpstreamRiscZero(bytes32 imageId, bytes32 journalDigest) public view {
        require(
            verifier.claimDigest(imageId, journalDigest)
                == ReceiptClaimLib.digest(ReceiptClaimLib.ok(imageId, journalDigest))
        );
    }

    function testZeroProverRejected() public {
        vm.expectRevert(ReceiptClaimVerifier.VerificationFailed.selector);
        new ReceiptClaimVerifier(address(0), ReceiptClaimLib.SYSTEM_STATE_ZERO_DIGEST);
        vm.expectRevert(ReceiptClaimVerifier.VerificationFailed.selector);
        new ReceiptClaimVerifier(prover, bytes32(0));
    }

    function testSingleTransactionSettlementAndTamperRejection() public {
        bytes memory journal = abi.encode(authorization());
        bytes32 journalDigest = sha256(journal);
        bytes memory seal = buildSeal(PROVER_KEY, IMAGE_ID, journalDigest);
        require(seal.length == 101);

        // Wrong selector reverts with SelectorMismatch
        bytes memory badSelector = bytes.concat(seal);
        badSelector[0] ^= 0x01;
        vm.expectRevert(
            abi.encodeWithSelector(
                ReceiptClaimVerifier.SelectorMismatch.selector, bytes4(badSelector), verifier.SELECTOR()
            )
        );
        verifier.verify(badSelector, IMAGE_ID, journalDigest);

        // Wrong imageId reverts
        vm.expectRevert(ReceiptClaimVerifier.VerificationFailed.selector);
        verifier.verify(seal, bytes32(uint256(IMAGE_ID) ^ 1), journalDigest);

        // Wrong journal digest reverts
        vm.expectRevert(ReceiptClaimVerifier.VerificationFailed.selector);
        verifier.verify(seal, IMAGE_ID, bytes32(uint256(journalDigest) ^ 1));

        // Mutated claim word or signature byte in seal reverts
        for (uint256 i = 4; i < seal.length; i++) {
            seal[i] ^= 0x01;
            vm.expectRevert();
            verifier.verify(seal, IMAGE_ID, journalDigest);
            seal[i] ^= 0x01;
        }

        // Wrong signer key reverts
        bytes memory wrongSignerSeal = buildSeal(OTHER_KEY, IMAGE_ID, journalDigest);
        vm.expectRevert(ReceiptClaimVerifier.VerificationFailed.selector);
        verifier.verify(wrongSignerSeal, IMAGE_ID, journalDigest);

        // Valid seal settles TaskEscrow in a single transaction
        vm.prank(RELAYER);
        escrow.settle(seal, journal);
        (,,,,,,, TaskEscrow.State s) = escrow.tasks(taskId);
        require(s == TaskEscrow.State.Paid && token.balanceOf(AGENT) == 100 && escrow.totalReserved() == 0);
    }
}
