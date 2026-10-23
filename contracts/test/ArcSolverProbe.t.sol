// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;
import {SolverBountyEscrow} from "../src/SolverBountyEscrow.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/// @dev Constructor-only eth_call probe for the solver path on Arc mainnet. Never broadcast this helper.
/// `scripts/check-arc-solver.py before` simulates its creation and expects the 32-byte return value 1.
/// `allowedChainId` is zero on mainnet. A local rehearsal passes its own chain ID instead.
contract ArcSolverProbe {
    constructor(address token, bytes memory seal, bytes32 imageId, bytes32 journalDigest, uint256 allowedChainId) {
        require(block.chainid == 5042 || (allowedChainId != 0 && block.chainid == allowedChainId), "Arc mainnet only");
        require(IERC20Metadata(token).decimals() == 6, "Expected six-decimal USDC interface");
        RiscZeroGroth16Verifier verifier =
            new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        verifier.verify(seal, imageId, journalDigest);
        (bool accepted,) = address(verifier)
            .staticcall(abi.encodeCall(verifier.verify, (seal, imageId, bytes32(uint256(journalDigest) ^ 1))));
        require(!accepted, "Changed journal accepted");
        SolverBountyEscrow escrow = new SolverBountyEscrow(token, address(verifier), imageId);
        require(address(escrow.token()) == token, "Escrow token differs");
        require(address(escrow.verifier()) == address(verifier), "Escrow verifier differs");
        require(escrow.imageId() == imageId, "Escrow image ID differs");
        require(escrow.totalReserved() == 0, "Fresh escrow has a reservation");
        require(IERC20Metadata(token).balanceOf(address(escrow)) == 0, "Fresh escrow holds tokens");
        // A recognizable result for the read-only constructor simulation.
        assembly {
            mstore(0, 1)
            return(0, 32)
        }
    }
}
