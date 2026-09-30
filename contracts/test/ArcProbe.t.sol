// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {RiscZeroGroth16Verifier} from "../vendor/risc0/contracts/src/groth16/RiscZeroGroth16Verifier.sol";
import {ControlID} from "../vendor/risc0/contracts/src/groth16/ControlID.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

/// @dev Constructor-only eth_call probe. Never broadcast this test helper.
contract ArcProbe {
    constructor(address token, bytes memory seal, bytes32 imageId, bytes32 journalDigest) {
        require(block.chainid == 5042002, "Arc testnet only");
        require(IERC20Metadata(token).decimals() == 6, "Expected six-decimal USDC interface");
        RiscZeroGroth16Verifier verifier =
            new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT, ControlID.BN254_CONTROL_ID);
        verifier.verify(seal, imageId, journalDigest);
        (bool accepted,) = address(verifier)
            .staticcall(abi.encodeCall(verifier.verify, (seal, imageId, bytes32(uint256(journalDigest) ^ 1))));
        require(!accepted, "Changed journal accepted");
        TaskEscrow escrow = new TaskEscrow(token, address(verifier), imageId);
        require(address(escrow.token()) == token && escrow.imageId() == imageId && escrow.totalReserved() == 0);
        require(IERC20Metadata(token).balanceOf(address(escrow)) == 0);
        InvoiceEscrow invoices = new InvoiceEscrow(token, address(verifier), imageId);
        require(address(invoices.token()) == token && invoices.imageId() == imageId && invoices.totalReserved() == 0);
        // A recognizable result for the read-only constructor simulation.
        assembly {
            mstore(0, 1)
            return(0, 32)
        }
    }
}
