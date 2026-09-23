// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {PolicyExecutionVault, IZkvmVerifier} from "./PolicyExecutionVault.sol";

/// @notice Purchase-order escrow settled by invoice authorizations, proven or signed.
/// @dev Same trust model as TaskEscrow: no administrator, one immutable verifier,
/// interpreter, signer and standard ERC-20 per deployment. One funded order backs
/// many invoices; each invoice's obligation ID pays at most once, and cumulative
/// payments never exceed the order's ceiling.
///
/// Two authenticators for the same 14-word journal:
/// - a RISC Zero receipt for the pinned invoice interpreter image (any amount);
/// - a signature from the immutable signer, which runs that interpreter natively
///   (only below `proofThreshold`, and only within the order's signer allowance).
/// A stolen signer key cannot redirect funds: the recipient is the order's vendor.
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
        /// @dev Ceiling on payments authorized by signature alone; 0 means proofs only.
        uint64 signerAllowance;
        uint64 signerSpent;
        bool signerRevoked;
    }

    // Rust InvoiceAuthorization::journal(): the twelve Authorization words, then
    // poId and poMaxTotal. Fourteen static ABI words.
    uint256 internal constant JOURNAL_LENGTH = 14 * 32;
    // Rust evidence::signer_digest(): sha256 over this tag, chain, escrow, sha256(journal).
    bytes internal constant SIGNER_TAG = "warrant/invoice-signer/v1";

    IERC20 public immutable token;
    IZkvmVerifier public immutable verifier;
    bytes32 public immutable imageId;
    /// @notice Address whose signatures settle sub-threshold invoices; zero disables that path.
    address public immutable signer;
    /// @notice Amounts at or above this need a proof; zero makes every payment need one.
    uint64 public immutable proofThreshold;
    uint256 public totalReserved;
    mapping(bytes32 => Order) public orders;
    /// @notice Obligation IDs are consumed across all orders and both authenticators.
    mapping(bytes32 => bool) public consumed;

    error InvalidTerms();
    error InvalidState();
    error Unauthorized();
    error InvalidAuthorization();
    error InvalidFunding();
    error AlreadyPaid();
    error OrderExceeded();
    error ProofRequired();
    error SignerAllowanceExceeded();

    event Offered(
        bytes32 indexed orderId,
        address indexed customer,
        address indexed recipient,
        bytes32 policyHash,
        bytes32 poId,
        uint64 policyVersion,
        uint64 maxTotal,
        uint64 signerAllowance,
        uint64 acceptBy,
        uint64 settleBy
    );
    event Accepted(bytes32 indexed orderId);
    event Paid(
        bytes32 indexed orderId,
        bytes32 indexed taskId,
        uint64 amount,
        bool proven,
        bytes32 deliverableHash,
        bytes32 evidenceHash
    );
    event SignerRevoked(bytes32 indexed orderId);
    event Closed(bytes32 indexed orderId, uint64 refunded);

    constructor(address token_, address verifier_, bytes32 imageId_, address signer_, uint64 proofThreshold_) {
        if (token_.code.length == 0 || verifier_.code.length == 0 || imageId_ == bytes32(0)) revert InvalidTerms();
        if (signer_ == address(0) && proofThreshold_ != 0) revert InvalidTerms();
        token = IERC20(token_);
        verifier = IZkvmVerifier(verifier_);
        imageId = imageId_;
        signer = signer_;
        proofThreshold = proofThreshold_;
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

    /// @notice What the signer must sign for `journal` to settle here and nowhere else.
    function signerDigest(bytes calldata journal) public view returns (bytes32) {
        return sha256(abi.encodePacked(SIGNER_TAG, uint256(block.chainid), address(this), sha256(journal)));
    }

    /// @notice Reserves the full order ceiling immediately.
    /// @param signerAllowance How much of the ceiling may be paid on signatures alone.
    function offer(
        bytes32 policyHash,
        uint64 policyVersion,
        bytes32 poId,
        address recipient,
        uint64 maxTotal,
        uint64 signerAllowance,
        uint64 acceptBy,
        uint64 settleBy
    ) external nonReentrant returns (bytes32 orderId) {
        orderId = orderIdFor(policyHash, poId);
        if (orders[orderId].state != State.Missing) revert InvalidState();
        if (
            recipient == address(0) || recipient == address(this) || policyHash == bytes32(0) || poId == bytes32(0)
                || policyVersion == 0 || maxTotal == 0 || signerAllowance > maxTotal || acceptBy < block.timestamp
                || settleBy <= acceptBy
        ) {
            revert InvalidTerms();
        }
        Order storage o = orders[orderId];
        o.customer = msg.sender;
        o.recipient = recipient;
        o.policyHash = policyHash;
        o.policyVersion = policyVersion;
        o.maxTotal = maxTotal;
        o.acceptBy = acceptBy;
        o.settleBy = settleBy;
        o.state = State.Offered;
        o.signerAllowance = signerAllowance;
        totalReserved += maxTotal;
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), maxTotal);
        if (token.balanceOf(address(this)) != beforeBalance + maxTotal) revert InvalidFunding();
        emit Offered(
            orderId,
            msg.sender,
            recipient,
            policyHash,
            poId,
            policyVersion,
            maxTotal,
            signerAllowance,
            acceptBy,
            settleBy
        );
    }

    /// @notice The vendor accepts the order's recorded terms; they have no update function.
    function accept(bytes32 orderId) external nonReentrant {
        Order storage o = orders[orderId];
        if (o.state != State.Offered || block.timestamp > o.acceptBy) revert InvalidState();
        if (msg.sender != o.recipient) revert Unauthorized();
        o.state = State.Accepted;
        emit Accepted(orderId);
    }

    /// @notice The customer turns off signature settlement for one order, for
    /// example after a suspected signer compromise. It only tightens: proven
    /// invoices still settle, so the vendor's guarantee is unchanged.
    function revokeSigner(bytes32 orderId) external {
        Order storage o = orders[orderId];
        if (o.state == State.Missing || o.state == State.Closed) revert InvalidState();
        if (msg.sender != o.customer) revert Unauthorized();
        o.signerRevoked = true;
        emit SignerRevoked(orderId);
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

    /// @notice Anyone can relay a proven authorization for any amount.
    function settle(bytes calldata seal, bytes calldata journal) external nonReentrant {
        (bytes32 orderId, Order storage o, PolicyExecutionVault.Authorization memory a) = _authorize(journal);
        verifier.verify(seal, imageId, sha256(journal));
        _pay(orderId, o, a, true);
    }

    /// @notice Anyone can relay a signed authorization below the proof threshold,
    /// within the order's signer allowance, unless the customer revoked signing.
    function settleSigned(bytes calldata journal, bytes calldata signature) external nonReentrant {
        (bytes32 orderId, Order storage o, PolicyExecutionVault.Authorization memory a) = _authorize(journal);
        if (signer == address(0) || a.amount >= proofThreshold) revert ProofRequired();
        if (o.signerRevoked) revert Unauthorized();
        if (a.amount > o.signerAllowance - o.signerSpent) revert SignerAllowanceExceeded();
        if (ECDSA.recover(signerDigest(journal), signature) != signer) revert Unauthorized();
        o.signerSpent += a.amount;
        _pay(orderId, o, a, false);
    }

    /// @dev Every check that does not depend on the authenticator.
    function _authorize(bytes calldata journal)
        internal
        view
        returns (bytes32 orderId, Order storage o, PolicyExecutionVault.Authorization memory a)
    {
        if (journal.length != JOURNAL_LENGTH) revert InvalidAuthorization();
        bytes32 poId;
        uint64 poMaxTotal;
        (a, poId, poMaxTotal) = abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64));
        orderId = orderIdFor(a.policyHash, poId);
        o = orders[orderId];
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
    }

    function _pay(bytes32 orderId, Order storage o, PolicyExecutionVault.Authorization memory a, bool proven) internal {
        consumed[a.taskId] = true;
        o.spent += a.amount;
        totalReserved -= a.amount;
        token.safeTransfer(o.recipient, a.amount);
        emit Paid(orderId, a.taskId, a.amount, proven, a.deliverableHash, a.evidenceHash);
    }
}
