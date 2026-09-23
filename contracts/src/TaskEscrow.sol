// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {PolicyExecutionVault, IZkvmVerifier} from "./PolicyExecutionVault.sol";

/// @notice Fixed-price task agreements with explicit acceptance and proof-based settlement.
/// @dev One immutable verifier, interpreter and standard ERC-20 per deployment. No administrator.
contract TaskEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum State {
        Missing,
        Offered,
        Accepted,
        Paid,
        Refunded
    }

    struct Task {
        address customer;
        address recipient;
        bytes32 policyHash;
        uint64 policyVersion;
        uint64 amount;
        uint64 acceptBy;
        uint64 settleBy;
        State state;
    }

    IERC20 public immutable token;
    IZkvmVerifier public immutable verifier;
    bytes32 public immutable imageId;
    uint256 public totalReserved;
    mapping(bytes32 => Task) public tasks;

    error InvalidTerms();
    error InvalidState();
    error Unauthorized();
    error InvalidAuthorization();
    error InvalidFunding();

    event Offered(
        bytes32 indexed taskId,
        address indexed customer,
        address indexed recipient,
        bytes32 policyHash,
        uint64 policyVersion,
        uint64 amount,
        uint64 acceptBy,
        uint64 settleBy
    );
    event Accepted(bytes32 indexed taskId);
    event Refunded(bytes32 indexed taskId);
    event Paid(bytes32 indexed taskId, bytes32 deliverableHash, bytes32 evidenceHash);

    constructor(address token_, address verifier_, bytes32 imageId_) {
        if (token_.code.length == 0 || verifier_.code.length == 0 || imageId_ == bytes32(0)) revert InvalidTerms();
        token = IERC20(token_);
        verifier = IZkvmVerifier(verifier_);
        imageId = imageId_;
    }

    /// @notice Funds are reserved immediately. IDs include the customer to prevent cross-customer squatting.
    function taskIdFor(address customer, bytes32 salt) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), customer, salt));
    }

    function offer(
        bytes32 salt,
        address recipient,
        bytes32 policyHash,
        uint64 policyVersion,
        uint64 amount,
        uint64 acceptBy,
        uint64 settleBy
    ) external nonReentrant returns (bytes32 taskId) {
        taskId = taskIdFor(msg.sender, salt);
        if (tasks[taskId].state != State.Missing) revert InvalidState();
        if (
            recipient == address(0) || recipient == address(this) || policyHash == bytes32(0) || policyVersion == 0
                || amount == 0 || acceptBy < block.timestamp || settleBy <= acceptBy
        ) {
            revert InvalidTerms();
        }
        tasks[taskId] =
            Task(msg.sender, recipient, policyHash, policyVersion, amount, acceptBy, settleBy, State.Offered);
        totalReserved += amount;
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), amount);
        if (token.balanceOf(address(this)) != beforeBalance + amount) revert InvalidFunding();
        emit Offered(taskId, msg.sender, recipient, policyHash, policyVersion, amount, acceptBy, settleBy);
    }

    /// @notice Acceptance commits to all recorded terms, which have no update function.
    function accept(bytes32 taskId) external nonReentrant {
        Task storage t = tasks[taskId];
        if (t.state != State.Offered || block.timestamp > t.acceptBy) revert InvalidState();
        if (msg.sender != t.recipient) revert Unauthorized();
        t.state = State.Accepted;
        emit Accepted(taskId);
    }

    /// @notice Customer may cancel only an unaccepted offer. After acceptance, only timeout permits refund.
    /// @dev Anyone may trigger timeout; funds always return to the recorded customer.
    function refund(bytes32 taskId) external nonReentrant {
        Task storage t = tasks[taskId];
        if (t.state == State.Offered) {
            if (block.timestamp <= t.acceptBy && msg.sender != t.customer) revert Unauthorized();
        } else if (t.state == State.Accepted) {
            if (block.timestamp <= t.settleBy) revert InvalidState();
        } else {
            revert InvalidState();
        }
        t.state = State.Refunded;
        totalReserved -= t.amount;
        token.safeTransfer(t.customer, t.amount);
        emit Refunded(taskId);
    }

    /// @notice Anyone can relay, but only the accepted recipient receives the fixed, reserved amount.
    function settle(bytes calldata seal, bytes calldata journal) external nonReentrant {
        if (journal.length != 384) revert InvalidAuthorization();
        PolicyExecutionVault.Authorization memory a = abi.decode(journal, (PolicyExecutionVault.Authorization));
        Task storage t = tasks[a.taskId];
        if (t.state != State.Accepted || block.timestamp > t.settleBy) revert InvalidState();
        if (
            a.policyHash != t.policyHash || a.policyVersion != t.policyVersion || a.chainId != block.chainid
                || a.vault != address(this) || a.token != address(token) || a.recipient != t.recipient
                || a.amount != t.amount || a.deliverableHash == bytes32(0) || a.evidenceHash == bytes32(0)
                || a.validAfter > a.validUntil || block.timestamp < a.validAfter || block.timestamp > a.validUntil
        ) {
            revert InvalidAuthorization();
        }
        verifier.verify(seal, imageId, sha256(journal));
        t.state = State.Paid;
        totalReserved -= t.amount;
        token.safeTransfer(t.recipient, t.amount);
        emit Paid(a.taskId, a.deliverableHash, a.evidenceHash);
    }
}
