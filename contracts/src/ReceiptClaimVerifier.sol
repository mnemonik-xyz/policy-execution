// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IZkvmVerifier} from "./PolicyExecutionVault.sol";

/// @notice Verifies selector-prefixed zkVM execution receipt claims and cryptographic prover seals.
contract ReceiptClaimVerifier is IZkvmVerifier {
    string public constant VERSION = "3.0.0";

    bytes32 internal constant OUTPUT_TAG = sha256("risc0.Output");
    bytes32 internal constant CLAIM_TAG = sha256("risc0.ReceiptClaim");
    bytes32 internal constant SEAL_TAG = sha256("warrant/receipt-claim/v1");

    bytes4 public immutable SELECTOR;
    address public immutable prover;
    bytes32 public immutable SYSTEM_STATE_ZERO_DIGEST;

    error SelectorMismatch(bytes4 received, bytes4 expected);
    error VerificationFailed();

    constructor(address prover_, bytes32 systemStateZeroDigest_) {
        if (prover_ == address(0) || systemStateZeroDigest_ == bytes32(0)) revert VerificationFailed();
        prover = prover_;
        SYSTEM_STATE_ZERO_DIGEST = systemStateZeroDigest_;
        SELECTOR = bytes4(sha256(abi.encodePacked(SEAL_TAG, VERSION, prover_)));
    }

    /// @notice Computes the RISC Zero ReceiptClaim digest for a successful (Halted, 0) guest execution.
    function claimDigest(bytes32 imageId, bytes32 journalDigest) public view returns (bytes32) {
        bytes32 outputDigest = sha256(abi.encodePacked(OUTPUT_TAG, journalDigest, bytes32(0), uint16(2) << 8));
        return sha256(
            abi.encodePacked(
                CLAIM_TAG,
                bytes32(0),
                imageId,
                SYSTEM_STATE_ZERO_DIGEST,
                outputDigest,
                uint32(0),
                uint32(0),
                uint16(4) << 8
            )
        );
    }

    /// @notice Computes the domain-separated receipt digest signed by the prover for this chain and verifier.
    function receiptDigest(bytes32 imageId, bytes32 journalDigest) public view returns (bytes32) {
        return sha256(
            abi.encodePacked(
                SEAL_TAG, uint256(block.chainid), address(this), SELECTOR, claimDigest(imageId, journalDigest)
            )
        );
    }

    /// @inheritdoc IZkvmVerifier
    function verify(bytes calldata seal, bytes32 imageId, bytes32 journalDigest) external view {
        if (seal.length != 101 || imageId == bytes32(0)) revert VerificationFailed();
        bytes4 received = bytes4(seal[:4]);
        if (received != SELECTOR) {
            revert SelectorMismatch(received, SELECTOR);
        }
        bytes32 expectedClaim = claimDigest(imageId, journalDigest);
        if (bytes32(seal[4:36]) != expectedClaim) revert VerificationFailed();
        bytes32 digest =
            sha256(abi.encodePacked(SEAL_TAG, uint256(block.chainid), address(this), SELECTOR, expectedClaim));
        if (ECDSA.recover(digest, seal[36:101]) != prover) revert VerificationFailed();
    }
}
