// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {PolicyExecutionVault, IZkvmVerifier} from "./PolicyExecutionVault.sol";

/// @notice Purchase-order escrow settled by proven invoice authorizations.
/// @dev Same trust model as TaskEscrow: no administrator, one immutable verifier,
/// interpreter and standard ERC-20 per deployment. One funded order backs many
/// invoices; each invoice's obligation ID pays at most once, and cumulative
/// payments never exceed the order's ceiling.
contract InvoiceEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum State {
        Missing,
        Offered,
        Accepted,
        Closed
    }

    struct Order {
        address customer;
        address recipient;
        bytes32 policyHash;
        uint64 policyVersion;
        uint64 maxTotal;
        uint64 spent;
        uint64 acceptBy;
        uint64 settleBy;
        State state;
    }

    // Rust InvoiceAuthorization::journal(): the twelve Authorization words, then
    // poId and poMaxTotal. Fourteen static ABI words.
    uint256 internal constant JOURNAL_LENGTH = 14 * 32;

    IERC20 public immutable token;
    IZkvmVerifier public immutable verifier;
    bytes32 public immutable imageId;
    uint256 public totalReserved;
    mapping(bytes32 => Order) public orders;
    /// @notice Obligation IDs are consumed across all orders, so an invoice cannot be paid twice.
    mapping(bytes32 => bool) public consumed;

    error InvalidTerms();
    error InvalidState();
    error Unauthorized();
    error InvalidAuthorization();
    error InvalidFunding();
    error AlreadyPaid();
    error OrderExceeded();

    event Offered(
        bytes32 indexed orderId,
        address indexed customer,
        address indexed recipient,
        bytes32 policyHash,
        bytes32 poId,
        uint64 policyVersion,
        uint64 maxTotal,
        uint64 acceptBy,
        uint64 settleBy
    );
    event Accepted(bytes32 indexed orderId);
    event Paid(
        bytes32 indexed orderId, bytes32 indexed taskId, uint64 amount, bytes32 deliverableHash, bytes32 evidenceHash
    );
    event Closed(bytes32 indexed orderId, uint64 refunded);

    constructor(address token_, address verifier_, bytes32 imageId_) {
        if (token_.code.length == 0 || verifier_.code.length == 0 || imageId_ == bytes32(0)) revert InvalidTerms();
        token = IERC20(token_);
        verifier = IZkvmVerifier(verifier_);
        imageId = imageId_;
    }

    /// @notice Order numbers are chosen by buyers and repeat across buyers; the
    /// policy commitment, which includes the buyer's key, keeps them apart.
    function orderIdFor(bytes32 policyHash, bytes32 poId) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), policyHash, poId));
    }

    function remaining(bytes32 orderId) external view returns (uint64) {
        Order storage o = orders[orderId];
        return o.maxTotal - o.spent;
    }

    /// @notice Reserves the full order ceiling immediately.
    function offer(
        bytes32 policyHash,
        uint64 policyVersion,
        bytes32 poId,
        address recipient,
        uint64 maxTotal,
        uint64 acceptBy,
        uint64 settleBy
    ) external nonReentrant returns (bytes32 orderId) {
        orderId = orderIdFor(policyHash, poId);
        if (orders[orderId].state != State.Missing) revert InvalidState();
        if (
            recipient == address(0) || recipient == address(this) || policyHash == bytes32(0) || poId == bytes32(0)
                || policyVersion == 0 || maxTotal == 0 || acceptBy < block.timestamp || settleBy <= acceptBy
        ) {
            revert InvalidTerms();
        }
        orders[orderId] =
            Order(msg.sender, recipient, policyHash, policyVersion, maxTotal, 0, acceptBy, settleBy, State.Offered);
        totalReserved += maxTotal;
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), maxTotal);
        if (token.balanceOf(address(this)) != beforeBalance + maxTotal) revert InvalidFunding();
        emit Offered(orderId, msg.sender, recipient, policyHash, poId, policyVersion, maxTotal, acceptBy, settleBy);
    }

    /// @notice The vendor accepts the order's recorded terms; they have no update function.
    function accept(bytes32 orderId) external nonReentrant {
        Order storage o = orders[orderId];
        if (o.state != State.Offered || block.timestamp > o.acceptBy) revert InvalidState();
        if (msg.sender != o.recipient) revert Unauthorized();
        o.state = State.Accepted;
        emit Accepted(orderId);
    }

    /// @notice Closes an order and returns what was not paid. The customer may cancel
    /// an unaccepted order; after acceptance only the settlement deadline closes it.
    /// @dev Anyone may trigger a timeout; funds always return to the recorded customer.
    function close(bytes32 orderId) external nonReentrant {
        Order storage o = orders[orderId];
        if (o.state == State.Offered) {
            if (block.timestamp <= o.acceptBy && msg.sender != o.customer) revert Unauthorized();
        } else if (o.state == State.Accepted) {
            if (block.timestamp <= o.settleBy) revert InvalidState();
        } else {
            revert InvalidState();
        }
        uint64 refund = o.maxTotal - o.spent;
        o.state = State.Closed;
        totalReserved -= refund;
        token.safeTransfer(o.customer, refund);
        emit Closed(orderId, refund);
    }

    /// @notice Anyone can relay. Only the accepted vendor is paid, only the proven
    /// amount, only once per invoice, and only within the order's ceiling.
    function settle(bytes calldata seal, bytes calldata journal) external nonReentrant {
        if (journal.length != JOURNAL_LENGTH) revert InvalidAuthorization();
        (PolicyExecutionVault.Authorization memory a, bytes32 poId, uint64 poMaxTotal) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64));
        bytes32 orderId = orderIdFor(a.policyHash, poId);
        Order storage o = orders[orderId];
        if (o.state != State.Accepted || block.timestamp > o.settleBy) revert InvalidState();
        if (
            a.policyVersion != o.policyVersion || a.chainId != block.chainid || a.vault != address(this)
                || a.token != address(token) || a.recipient != o.recipient || poMaxTotal != o.maxTotal || a.amount == 0
                || a.taskId == bytes32(0) || a.deliverableHash == bytes32(0) || a.evidenceHash == bytes32(0)
                || a.validAfter > a.validUntil || block.timestamp < a.validAfter || block.timestamp > a.validUntil
        ) {
            revert InvalidAuthorization();
        }
        if (consumed[a.taskId]) revert AlreadyPaid();
        if (a.amount > o.maxTotal - o.spent) revert OrderExceeded();
        verifier.verify(seal, imageId, sha256(journal));
        consumed[a.taskId] = true;
        o.spent += a.amount;
        totalReserved -= a.amount;
        token.safeTransfer(o.recipient, a.amount);
        emit Paid(orderId, a.taskId, a.amount, a.deliverableHash, a.evidenceHash);
    }
}
