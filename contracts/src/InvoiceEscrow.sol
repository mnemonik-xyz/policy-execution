// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {PolicyExecutionVault, IZkvmVerifier} from "./PolicyExecutionVault.sol";

/// @notice Purchase-order escrow settled by invoice authorizations: proven, signed
/// or approved by the buyer.
/// @dev Same trust model as TaskEscrow: no administrator, one immutable verifier,
/// interpreter and standard ERC-20 per deployment. One funded order backs many
/// invoices; each customer pays an obligation ID at most once, and cumulative
/// payments never exceed the order's ceiling. Every path pays the order's
/// accepted vendor and nobody else.
///
/// Three authenticators:
/// - `settle`: a RISC Zero receipt for the pinned invoice interpreter image, any amount;
/// - `settleSigned`: a signature from the signer the buyer named for this order,
///   which runs that interpreter natively; only below the order's proof threshold
///   and within its signer allowance. The buyer can revoke it per order.
/// - `settleApproved`: the buyer settles an invoice the interpreter could not decide
///   on their own authority, within the same ceiling and replay protection.
///   The contract does not require a prior Ask result.
contract InvoiceEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum State {
        Missing,
        Offered,
        Accepted,
        Closed
    }

    enum Authenticator {
        Proof,
        Signature,
        BuyerApproval
    }

    /// @notice Everything the buyer fixes at `offer`; the vendor accepts exactly this.
    struct Terms {
        bytes32 policyHash;
        uint64 policyVersion;
        bytes32 poId;
        address recipient;
        uint64 maxTotal;
        /// @dev Whose signatures settle sub-threshold invoices; zero means proofs and approvals only.
        address signer;
        /// @dev Ceiling on payments authorized by signature alone.
        uint64 signerAllowance;
        /// @dev Amounts at or above this need a proof or the buyer's approval.
        uint64 proofThreshold;
        uint64 acceptBy;
        uint64 settleBy;
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
        address signer;
        uint64 signerAllowance;
        uint64 signerSpent;
        uint64 proofThreshold;
        bool signerRevoked;
    }

    // Rust InvoiceAuthorization::journal(): the twelve Authorization words, then
    // poId, poMaxTotal and customer. Fifteen static ABI words.
    uint256 internal constant JOURNAL_LENGTH = 15 * 32;
    // Rust evidence::signer_digest(): sha256 over this tag, chain, escrow, sha256(journal).
    bytes internal constant SIGNER_TAG = "warrant/invoice-signer/v1";

    IERC20 public immutable token;
    IZkvmVerifier public immutable verifier;
    bytes32 public immutable imageId;
    uint256 public totalReserved;
    mapping(bytes32 => Order) internal orders;
    /// @notice Each customer consumes an obligation across their orders and authenticators.
    mapping(address => mapping(bytes32 => bool)) public consumed;

    error InvalidTerms();
    error InvalidState();
    error Unauthorized();
    error InvalidAuthorization();
    error InvalidFunding();
    error AlreadyPaid();
    error OrderExceeded();
    error ProofRequired();
    error SignerAllowanceExceeded();

    event Offered(bytes32 indexed orderId, address indexed customer, Terms terms);
    event Accepted(bytes32 indexed orderId);
    event Paid(
        bytes32 indexed orderId,
        bytes32 indexed taskId,
        uint64 amount,
        Authenticator authenticator,
        bytes32 deliverableHash,
        bytes32 evidenceHash
    );
    event SignerRevoked(bytes32 indexed orderId);
    event Closed(bytes32 indexed orderId, uint64 refunded);

    constructor(address token_, address verifier_, bytes32 imageId_) {
        if (token_.code.length == 0 || verifier_.code.length == 0 || imageId_ == bytes32(0)) revert InvalidTerms();
        token = IERC20(token_);
        verifier = IZkvmVerifier(verifier_);
        imageId = imageId_;
    }

    /// @notice The funding customer owns the order namespace, even if another customer
    /// copies the policy commitment and PO identifier from a pending offer.
    function orderIdFor(address customer, bytes32 policyHash, bytes32 poId) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), customer, policyHash, poId));
    }

    function order(bytes32 orderId) external view returns (Order memory) {
        return orders[orderId];
    }

    function remaining(bytes32 orderId) external view returns (uint64) {
        Order storage o = orders[orderId];
        return o.maxTotal - o.spent;
    }

    /// @notice What a signer must sign for `journal` to settle here and nowhere else.
    function signerDigest(bytes calldata journal) public view returns (bytes32) {
        return sha256(abi.encodePacked(SIGNER_TAG, uint256(block.chainid), address(this), sha256(journal)));
    }

    /// @notice Reserves the full order ceiling immediately. The signer, its allowance
    /// and the proof threshold are the buyer's choice for this order alone.
    function offer(Terms calldata t) external nonReentrant returns (bytes32 orderId) {
        orderId = orderIdFor(msg.sender, t.policyHash, t.poId);
        if (orders[orderId].state != State.Missing) revert InvalidState();
        if (
            t.recipient == address(0) || t.recipient == address(this) || t.policyHash == bytes32(0)
                || t.poId == bytes32(0) || t.policyVersion == 0 || t.maxTotal == 0 || t.signerAllowance > t.maxTotal
                || t.acceptBy < block.timestamp || t.settleBy <= t.acceptBy
        ) {
            revert InvalidTerms();
        }
        // A signer without room to act, or room without a signer, is a mistake; the
        // vendor cannot be the signer of its own invoices.
        if (t.signer == address(0)) {
            if (t.signerAllowance != 0 || t.proofThreshold != 0) revert InvalidTerms();
        } else if (t.signerAllowance == 0 || t.proofThreshold == 0 || t.signer == t.recipient) {
            revert InvalidTerms();
        }
        Order storage o = orders[orderId];
        o.customer = msg.sender;
        o.recipient = t.recipient;
        o.policyHash = t.policyHash;
        o.policyVersion = t.policyVersion;
        o.maxTotal = t.maxTotal;
        o.acceptBy = t.acceptBy;
        o.settleBy = t.settleBy;
        o.state = State.Offered;
        o.signer = t.signer;
        o.signerAllowance = t.signerAllowance;
        o.proofThreshold = t.proofThreshold;
        totalReserved += t.maxTotal;
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), t.maxTotal);
        if (token.balanceOf(address(this)) != beforeBalance + t.maxTotal) revert InvalidFunding();
        emit Offered(orderId, msg.sender, t);
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
    /// example after a suspected signer compromise. It only tightens: proven and
    /// approved invoices remain available subject to evidence and settlement deadlines.
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
        _pay(orderId, o, a.taskId, a.amount, Authenticator.Proof, a.deliverableHash, a.evidenceHash);
    }

    /// @notice Anyone can relay an authorization signed by the order's signer, below
    /// the order's proof threshold and within its signer allowance, unless the
    /// customer revoked signing.
    function settleSigned(bytes calldata journal, bytes calldata signature) external nonReentrant {
        (bytes32 orderId, Order storage o, PolicyExecutionVault.Authorization memory a) = _authorize(journal);
        if (o.signer == address(0) || a.amount >= o.proofThreshold) revert ProofRequired();
        if (o.signerRevoked) revert Unauthorized();
        if (a.amount > o.signerAllowance - o.signerSpent) revert SignerAllowanceExceeded();
        if (ECDSA.recover(signerDigest(journal), signature) != o.signer) revert Unauthorized();
        o.signerSpent += a.amount;
        _pay(orderId, o, a.taskId, a.amount, Authenticator.Signature, a.deliverableHash, a.evidenceHash);
    }

    /// @notice The customer pays an invoice the interpreter left undecided (Ask) on
    /// their own authority. The same bounds apply: the order's vendor, its ceiling
    /// and its deadline, and the obligation pays once across this customer's orders
    /// and every path. No Ask result is required on chain.
    /// @param obligationId `evidence::obligation_id` of the invoice: seller tax ID and number.
    /// @param documentHash sha256 of the invoice bytes, for the record.
    function settleApproved(bytes32 orderId, bytes32 obligationId, uint64 amount, bytes32 documentHash)
        external
        nonReentrant
    {
        Order storage o = orders[orderId];
        if (o.state != State.Accepted || block.timestamp > o.settleBy) revert InvalidState();
        if (msg.sender != o.customer) revert Unauthorized();
        if (amount == 0 || obligationId == bytes32(0) || documentHash == bytes32(0)) revert InvalidAuthorization();
        if (consumed[o.customer][obligationId]) revert AlreadyPaid();
        if (amount > o.maxTotal - o.spent) revert OrderExceeded();
        _pay(orderId, o, obligationId, amount, Authenticator.BuyerApproval, documentHash, bytes32(0));
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
        address customer;
        (a, poId, poMaxTotal, customer) =
            abi.decode(journal, (PolicyExecutionVault.Authorization, bytes32, uint64, address));
        orderId = orderIdFor(customer, a.policyHash, poId);
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
        if (consumed[o.customer][a.taskId]) revert AlreadyPaid();
        if (a.amount > o.maxTotal - o.spent) revert OrderExceeded();
    }

    function _pay(
        bytes32 orderId,
        Order storage o,
        bytes32 taskId,
        uint64 amount,
        Authenticator authenticator,
        bytes32 deliverableHash,
        bytes32 evidenceHash
    ) internal {
        consumed[o.customer][taskId] = true;
        o.spent += amount;
        totalReserved -= amount;
        token.safeTransfer(o.recipient, amount);
        emit Paid(orderId, taskId, amount, authenticator, deliverableHash, evidenceHash);
    }
}
