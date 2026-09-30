// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";

interface ProofVm {
    function readFile(string calldata) external view returns (string memory);
    function parseJsonBytes(string calldata, string calldata) external pure returns (bytes memory);
    function parseJsonBytes32(string calldata, string calldata) external pure returns (bytes32);
    function expectRevert() external;
    function envOr(string calldata, string calldata) external view returns (string memory);
}

contract RealVerifierTest {
    ProofVm constant vm = ProofVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function testRealReceiptAndTampering() public {
        string memory path = vm.envOr("WARRANT_EVM_FIXTURE", string("test/fixtures/risc0-3.0.5.json"));
        string memory json = vm.readFile(path);
        bytes memory seal = vm.parseJsonBytes(json, ".seal");
        bytes memory journal = vm.parseJsonBytes(json, ".journal");
        bytes32 imageId = vm.parseJsonBytes32(json, ".imageId");
        RiscZeroGroth16Verifier verifier =
            new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        verifier.verify(seal, imageId, sha256(journal));
        bytes32 digest = sha256(journal);
        vm.expectRevert();
        verifier.verify(seal, bytes32(uint256(imageId) ^ 1), digest);
        require(journal.length > 0 && journal.length % 32 == 0);
        for (uint256 word; word < journal.length / 32; word++) {
            journal[word * 32 + 31] ^= bytes1(uint8(1));
            digest = sha256(journal);
            vm.expectRevert();
            verifier.verify(seal, imageId, digest);
            journal[word * 32 + 31] ^= bytes1(uint8(1));
        }
        seal[seal.length - 1] ^= bytes1(uint8(1));
        digest = sha256(journal);
        vm.expectRevert();
        verifier.verify(seal, imageId, digest);
    }
}
