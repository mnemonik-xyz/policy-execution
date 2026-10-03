// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {TestToken} from "./PolicyExecutionVault.t.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";

interface InvoiceProofVm {
    function readFile(string calldata) external view returns (string memory);
    function parseJsonBytes(string calldata, string calldata) external pure returns (bytes memory);
    function parseJsonBytes32(string calldata, string calldata) external pure returns (bytes32);
    function chainId(uint256) external;
    function warp(uint256) external;
    function etch(address, bytes calldata) external;
    function prank(address) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
}

contract RealInvoiceSettlementTest {
    InvoiceProofVm constant vm = InvoiceProofVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function testRealAttestedInvoiceSettlementAndReplay() public {
        string memory json = vm.readFile("test/fixtures/invoice-proof.json");
        bytes memory seal = vm.parseJsonBytes(json, ".seal");
        bytes memory journal = vm.parseJsonBytes(json, ".journal");
        bytes32 imageId = vm.parseJsonBytes32(json, ".imageId");
        require(journal.length == 15 * 32);
        (PolicyExecutionVault.Authorization memory a, bytes32 poId, uint64 ceiling, address customer) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64, address));
        RiscZeroGroth16Verifier verifier =
            new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);

        verifier.verify(seal, imageId, sha256(journal));
        for (uint256 word; word < 15; word++) {
            journal[word * 32 + 31] ^= bytes1(uint8(1));
            bytes32 tamperedDigest = sha256(journal);
            vm.expectRevert();
            verifier.verify(seal, imageId, tamperedDigest);
            journal[word * 32 + 31] ^= bytes1(uint8(1));
        }

        // Reconstruct the fixture's local payment domain. Only runtime addresses
        // are installed with cheatcodes; funding, acceptance and payment execute
        // the actual contract code and the cryptographic verifier.
        vm.chainId(a.chainId);
        vm.warp(a.validAfter);
        TestToken tokenImplementation = new TestToken();
        vm.etch(a.token, address(tokenImplementation).code);
        TestToken token = TestToken(a.token);
        InvoiceEscrow implementation = new InvoiceEscrow(a.token, address(verifier), imageId);
        vm.etch(a.vault, address(implementation).code);
        InvoiceEscrow escrow = InvoiceEscrow(a.vault);
        token.mint(customer, ceiling);
        vm.prank(customer);
        token.approve(a.vault, ceiling);
        vm.prank(customer);
        bytes32 orderId = escrow.offer(
            InvoiceEscrow.Terms(
                a.policyHash,
                a.policyVersion,
                poId,
                a.recipient,
                ceiling,
                address(0),
                0,
                0,
                a.validAfter + 1,
                a.validUntil
            )
        );
        vm.prank(a.recipient);
        escrow.accept(orderId);
        vm.prank(address(0xCAFE));
        escrow.settle(seal, journal);
        require(token.balanceOf(a.recipient) == a.amount);
        require(escrow.remaining(orderId) == ceiling - a.amount);
        require(escrow.totalReserved() == ceiling - a.amount);
        require(escrow.consumed(customer, a.taskId));
        vm.expectRevert(InvoiceEscrow.AlreadyPaid.selector);
        escrow.settle(seal, journal);
    }
}
