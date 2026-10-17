// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {TaskEscrow} from "../src/TaskEscrow.sol";
import {ReceiptClaimVerifier} from "../src/ReceiptClaimVerifier.sol";

interface DeployReceiptVm {
    function envAddress(string calldata) external returns (address);
    function envOr(string calldata, address) external returns (address);
    function envOr(string calldata, bytes32) external returns (bytes32);
    function startBroadcast() external;
    function stopBroadcast() external;
}

/// @notice Deploys ReceiptClaimVerifier and TaskEscrow for receipt-claim settlement.
contract DeployReceipt {
    DeployReceiptVm constant vm = DeployReceiptVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    bytes32 constant DEFAULT_IMAGE_ID = 0xafd4ad2d38c10243e72193f92a5d1ab23f3d2135f32282a9c1e43bf734dc105e;

    event Deployment(address verifier, address escrow, address token, bytes32 imageId, address prover, uint256 chainId);

    function run() external returns (ReceiptClaimVerifier verifier, TaskEscrow escrow) {
        address token = vm.envAddress("WARRANT_TOKEN");
        address prover = vm.envOr("WARRANT_SELLER", msg.sender);
        bytes32 imageId = vm.envOr("WARRANT_IMAGE_ID", DEFAULT_IMAGE_ID);
        bytes32 defaultSystemStateDigest =
            sha256(abi.encodePacked(sha256("risc0.SystemState"), bytes32(0), uint32(0), uint16(1) << 8));
        bytes32 systemStateZeroDigest = vm.envOr("WARRANT_SYSTEM_STATE_DIGEST", defaultSystemStateDigest);
        vm.startBroadcast();
        verifier = new ReceiptClaimVerifier(prover, systemStateZeroDigest);
        escrow = new TaskEscrow(token, address(verifier), imageId);
        vm.stopBroadcast();
        emit Deployment(address(verifier), address(escrow), token, imageId, prover, block.chainid);
    }
}
