// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {ProofInvoiceEscrow, IInvoiceReplayRegistry} from "./ProofInvoiceEscrow.sol";

/// @notice Immutable token/verifier family and shared customer obligation history.
/// @dev Only escrows created from this exact code may consume. There is no admin,
/// registration setter or deletion. A new guest image can use the SAME factory
/// without resetting replay history. Customers must approve the new image before
/// funding it; creation is permissionless and does not endorse an image.
contract ProofInvoiceFactory is IInvoiceReplayRegistry {
    address public immutable token;
    address public immutable verifier;
    mapping(address => bool) public isEscrow;
    mapping(address => mapping(bytes32 => bool)) public consumed;
    error UnauthorizedEscrow();
    error AlreadyConsumed();
    event EscrowCreated(address indexed escrow, bytes32 indexed imageId);

    constructor(address token_, address verifier_) {
        require(token_.code.length != 0 && verifier_.code.length != 0, "invalid dependencies");
        token = token_;
        verifier = verifier_;
    }

    function createEscrow(bytes32 imageId) external returns (ProofInvoiceEscrow escrow) {
        escrow = new ProofInvoiceEscrow(token, verifier, imageId, address(this));
        isEscrow[address(escrow)] = true;
        emit EscrowCreated(address(escrow), imageId);
    }

    function consume(address customer, bytes32 obligation) external {
        if (!isEscrow[msg.sender]) revert UnauthorizedEscrow();
        if (consumed[customer][obligation]) revert AlreadyConsumed();
        consumed[customer][obligation] = true;
    }
}
