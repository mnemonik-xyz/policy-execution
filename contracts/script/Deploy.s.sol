// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";

interface DeployVm {
    function envAddress(string calldata) external returns (address);
    function envBytes32(string calldata) external returns (bytes32);
    function startBroadcast() external;
    function stopBroadcast() external;
}

/// @dev Uses Foundry's --account/--sender signing configuration; no private key in source.
contract Deploy {
    DeployVm constant vm = DeployVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    event Deployment(address verifier, address escrow, address token, bytes32 imageId, uint256 chainId);

    function run() external returns (RiscZeroGroth16Verifier verifier, TaskEscrow escrow) {
        address token = vm.envAddress("WARRANT_TOKEN");
        bytes32 imageId = vm.envBytes32("WARRANT_IMAGE_ID");
        vm.startBroadcast();
        verifier = new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        escrow = new TaskEscrow(token, address(verifier), imageId);
        vm.stopBroadcast();
        emit Deployment(address(verifier), address(escrow), token, imageId, block.chainid);
    }
}
