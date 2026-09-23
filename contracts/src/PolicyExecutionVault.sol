// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @dev ABI-compatible with RISC Zero's IRiscZeroVerifier.verify. Invalid proofs revert.
interface IZkvmVerifier {
    function verify(bytes calldata seal, bytes32 imageId, bytes32 journalDigest) external view;
}

/// @notice Customer-controlled spending vault, not an irrevocable task escrow.
/// @dev Requires a standard non-rebasing, non-fee-on-transfer ERC-20. No proxy or verifier setter.
contract PolicyExecutionVault is Ownable2Step, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // Exact field order and widths of Rust Authorization::journal(): twelve ABI words.
    struct Authorization {
        bytes32 policyHash;
        uint64 chainId;
        address vault;
        address token;
        address recipient;
        uint64 amount;
        bytes32 taskId;
        bytes32 deliverableHash;
        uint64 policyVersion;
        uint64 validAfter;
        uint64 validUntil;
        bytes32 evidenceHash;
    }

    IZkvmVerifier public immutable verifier;
    bytes32 public immutable imageId;
    IERC20 public immutable token;
    uint64 public immutable hardMaxPayment;
    bytes32 public policyHash;
    uint64 public policyVersion;
    uint256 public totalBudget;
    uint256 public totalSpent;
    mapping(bytes32 => bool) public consumed;
    mapping(bytes32 => bool) public cancelled;

    error InvalidConfiguration();
    error InvalidJournal();
    error InvalidPolicy();
    error InvalidDomain();
    error InvalidPayment();
    error InvalidWindow();
    error TaskUnavailable();
    error BudgetExceeded();

    event PolicySet(uint64 indexed version, bytes32 indexed policyHash);
    event BudgetSet(uint256 totalBudget);
    event TaskCancelled(bytes32 indexed taskId);
    event Withdrawn(address indexed customer, uint256 amount);
    event Paid(
        bytes32 indexed taskId,
        address indexed recipient,
        uint64 amount,
        bytes32 policyHash,
        uint64 policyVersion,
        bytes32 deliverableHash,
        bytes32 evidenceHash
    );

    constructor(
        address customer,
        address verifier_,
        bytes32 imageId_,
        address token_,
        uint64 hardMaxPayment_,
        uint256 budget_
    ) Ownable(customer) {
        if (verifier_.code.length == 0 || token_.code.length == 0 || imageId_ == bytes32(0) || hardMaxPayment_ == 0) revert InvalidConfiguration();
        verifier = IZkvmVerifier(verifier_);
        imageId = imageId_;
        token = IERC20(token_);
        hardMaxPayment = hardMaxPayment_;
        totalBudget = budget_;
    }

    /// @notice Approve a policy already constructed for policyVersion + 1 and this vault's domain.
    /// @dev Zero revokes authorization. Every update advances the version; no old proof revival.
    function setPolicy(bytes32 commitment) external onlyOwner {
        policyVersion += 1;
        policyHash = commitment;
        emit PolicySet(policyVersion, commitment);
    }

    /// @notice Lifetime allowance. Deposits, withdrawals and policy changes never reset spent.
    function setBudget(uint256 budget) external onlyOwner {
        if (budget < totalSpent) revert InvalidConfiguration();
        totalBudget = budget;
        emit BudgetSet(budget);
    }

    /// @notice Permanent across policy versions. Cancellation is not proof that a task was paid.
    function cancelTask(bytes32 taskId) external onlyOwner {
        if (taskId == bytes32(0) || consumed[taskId] || cancelled[taskId]) revert TaskUnavailable();
        cancelled[taskId] = true;
        emit TaskCancelled(taskId);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function withdraw(uint256 amount) external onlyOwner nonReentrant {
        token.safeTransfer(owner(), amount);
        emit Withdrawn(owner(), amount);
    }

    /// @notice Anyone can relay a proof; msg.sender never selects the payout address or amount.
    function pay(bytes calldata seal, bytes calldata journal) external nonReentrant whenNotPaused {
        if (journal.length != 384) revert InvalidJournal();
        Authorization memory a = abi.decode(journal, (Authorization));
        if (policyHash == bytes32(0) || a.policyHash != policyHash || a.policyVersion != policyVersion) {
            revert InvalidPolicy();
        }
        if (a.chainId != block.chainid || a.vault != address(this) || a.token != address(token)) {
            revert InvalidDomain();
        }
        if (
            a.recipient == address(0) || a.amount == 0 || a.amount > hardMaxPayment || a.taskId == bytes32(0)
                || a.deliverableHash == bytes32(0) || a.evidenceHash == bytes32(0)
        ) {
            revert InvalidPayment();
        }
        if (a.validAfter > a.validUntil || block.timestamp < a.validAfter || block.timestamp > a.validUntil) {
            revert InvalidWindow();
        }
        if (consumed[a.taskId] || cancelled[a.taskId]) revert TaskUnavailable();
        if (a.amount > totalBudget - totalSpent) revert BudgetExceeded();

        verifier.verify(seal, imageId, sha256(journal));
        consumed[a.taskId] = true;
        totalSpent += a.amount;
        token.safeTransfer(a.recipient, a.amount);
        emit Paid(a.taskId, a.recipient, a.amount, a.policyHash, a.policyVersion, a.deliverableHash, a.evidenceHash);
    }
}
