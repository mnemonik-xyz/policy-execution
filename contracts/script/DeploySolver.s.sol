// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {SolverBountyEscrow} from "../src/SolverBountyEscrow.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";

interface SolverDeployVm {
    function envAddress(string calldata name) external returns (address);
    function envBytes32(string calldata name) external returns (bytes32);
    function startBroadcast() external;
    function stopBroadcast() external;
}

contract DeploySolver {
    SolverDeployVm constant vm = SolverDeployVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function run() external returns (SolverBountyEscrow escrow) {
        address token = vm.envAddress("WARRANT_TOKEN");
        bytes32 imageId = vm.envBytes32("WARRANT_SOLVER_IMAGE_ID");
        vm.startBroadcast();
        RiscZeroGroth16Verifier verifier =
            new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        escrow = new SolverBountyEscrow(token, address(verifier), imageId);
        vm.stopBroadcast();
    }
}
