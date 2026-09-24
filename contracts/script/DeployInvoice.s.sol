// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";

interface DeployInvoiceVm {
    function envAddress(string calldata) external returns (address);
    function envBytes32(string calldata) external returns (bytes32);
    function startBroadcast() external;
    function stopBroadcast() external;
}

/// @dev Same signing configuration as Deploy.s.sol: --account/--sender, no private key in source.
contract DeployInvoice {
    DeployInvoiceVm constant vm = DeployInvoiceVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    event Deployment(address verifier, address escrow, address token, bytes32 imageId, uint256 chainId);

    function run() external returns (RiscZeroGroth16Verifier verifier, InvoiceEscrow escrow) {
        address token = vm.envAddress("WARRANT_TOKEN");
        bytes32 imageId = vm.envBytes32("WARRANT_IMAGE_ID");
        // Signers, allowances and proof thresholds are chosen per order at offer time.
        vm.startBroadcast();
        verifier = new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        escrow = new InvoiceEscrow(token, address(verifier), imageId);
        vm.stopBroadcast();
        emit Deployment(address(verifier), address(escrow), token, imageId, block.chainid);
    }
}
