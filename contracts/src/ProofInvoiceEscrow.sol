// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {InvoiceEscrow} from "./InvoiceEscrow.sol";

interface IInvoiceReplayRegistry {
    function consume(address customer, bytes32 obligation) external;
}

/// @notice Invoice escrow with no signature or buyer-approval payment bypass.
/// @dev Refunds follow InvoiceEscrow.close. Escrows created by one ProofInvoiceFactory
/// share replay history, including when customers choose a new guest image.
contract ProofInvoiceEscrow is InvoiceEscrow {
    IInvoiceReplayRegistry public immutable replayRegistry;

    constructor(address token_, address verifier_, bytes32 imageId_, address registry_)
        InvoiceEscrow(token_, verifier_, imageId_)
    {
        require(registry_.code.length != 0, "registry required");
        replayRegistry = IInvoiceReplayRegistry(registry_);
    }

    function proofOnly() external pure returns (bool) {
        return true;
    }

    function offer(Terms calldata t) public override returns (bytes32) {
        if (t.signer != address(0) || t.signerAllowance != 0 || t.proofThreshold != 0) revert ProofRequired();
        return super.offer(t);
    }

    function settleSigned(bytes calldata, bytes calldata) external pure override {
        revert ProofRequired();
    }

    function settleApproved(bytes32, bytes32, uint64, bytes32) external pure override {
        revert ProofRequired();
    }

    // Keep the invariant at the transfer boundary as well as at the public entries.
    function _pay(
        bytes32 orderId,
        Order storage o,
        bytes32 taskId,
        uint64 amount,
        Authenticator authenticator,
        bytes32 deliverableHash,
        bytes32 evidenceHash
    ) internal override {
        if (authenticator != Authenticator.Proof) revert ProofRequired();
        replayRegistry.consume(o.customer, taskId);
        super._pay(orderId, o, taskId, amount, authenticator, deliverableHash, evidenceHash);
    }
}
