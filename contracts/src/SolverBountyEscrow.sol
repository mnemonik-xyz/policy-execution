// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {TaskEscrow} from "./TaskEscrow.sol";
import {PolicyExecutionVault} from "./PolicyExecutionVault.sol";

/// @notice Fixed-price solver tasks. A successful payment must publish the proven result.
/// @dev Pins a solver guest at deployment. No signer, reviewer, approval or hash-only settlement path.
contract SolverBountyEscrow is TaskEscrow {
    // ASCII "warrant/schedule/v1", u32 count, up to 128 (u32, u32, u64) assignments.
    uint256 public constant MAX_RESULT_BYTES = 19 + 4 + 128 * 16;
    event ResultPublished(bytes32 indexed taskId, bytes32 indexed resultHash, uint64 totalCost, bytes result);
    error ResultRequired();
    error InvalidResult();

    constructor(address token_, address verifier_, bytes32 imageId_) TaskEscrow(token_, verifier_, imageId_) {}

    /// @notice Disable the inherited hash-only entry point, including calls through the TaskEscrow ABI.
    function settle(bytes calldata, bytes calldata) external pure override {
        revert ResultRequired();
    }

    /// @notice Any relayer can deliver and settle. The accepted seller always receives the payment.
    /// Result bytes become public in calldata before finality; this is not confidential fair exchange.
    function settleWithResult(bytes calldata seal, bytes calldata journal, bytes calldata result)
        external
        nonReentrant
    {
        if (journal.length != 416) revert InvalidAuthorization();
        (PolicyExecutionVault.Authorization memory a, uint64 totalCost) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, uint64));
        if (result.length < 39 || result.length > MAX_RESULT_BYTES || sha256(result) != a.deliverableHash) {
            revert InvalidResult();
        }
        // The proof authenticates the whole 13-word journal, including totalCost.
        // The guest checks canonical encoding, completeness and every scheduling constraint.
        _settle(seal, journal, a);
        emit ResultPublished(a.taskId, a.deliverableHash, totalCost, result);
    }
}
