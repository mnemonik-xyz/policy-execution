// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {TaskEscrow} from "../src/TaskEscrow.sol";
import {JournalVerifier} from "../test/PolicyExecutionVault.t.sol";

interface DeployVm {
    function envAddress(string calldata) external returns (address);
    function startBroadcast() external;
    function stopBroadcast() external;
}

/// @notice Deploys JournalVerifier and TaskEscrow for showcase testnet mock settlement.
contract DeployMock {
    DeployVm constant vm = DeployVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    event Deployment(address verifier, address escrow, address token, bytes32 imageId, uint256 chainId);

    function run() external returns (JournalVerifier verifier, TaskEscrow escrow) {
        address token = vm.envAddress("WARRANT_TOKEN");
        bytes32 mockImageId = bytes32(uint256(1));
        vm.startBroadcast();
        verifier = new JournalVerifier();
        escrow = new TaskEscrow(token, address(verifier), mockImageId);
        vm.stopBroadcast();
        emit Deployment(address(verifier), address(escrow), token, mockImageId, block.chainid);
    }
}
