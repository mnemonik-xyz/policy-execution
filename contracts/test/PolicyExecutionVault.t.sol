// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {PolicyExecutionVault, IZkvmVerifier} from "../src/PolicyExecutionVault.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

interface Vm {
    function prank(address) external;
    function warp(uint256) external;
    function expectRevert() external;
}

// State-machine tests only: this verifier is deliberately not a cryptographic verifier.
contract JournalVerifier is IZkvmVerifier {
    mapping(bytes32 => bool) public approved;

    function approve(bytes memory journal) external {
        approved[sha256(journal)] = true;
    }

    function verify(bytes calldata seal, bytes32 imageId, bytes32 digest) external view {
        require(keccak256(seal) == keccak256(hex"abcd") && imageId == bytes32(uint256(1)));
        require(approved[digest], "unproved journal");
    }
}

contract TestToken is ERC20 {
    bool public fail;
    constructor() ERC20("Test", "TEST") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function setFail(bool value) external {
        fail = value;
    }

    function transfer(address to, uint256 amount) public virtual override returns (bool) {
        if (fail) return false;
        return super.transfer(to, amount);
    }
}

contract PolicyExecutionVaultTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant CUSTOMER = address(0x1234);
    address constant RELAYER = address(0x5678);
    address constant RECIPIENT = address(0x9999);
    bytes32 constant POLICY = keccak256("policy");
    PolicyExecutionVault vault;
    JournalVerifier verifier;
    TestToken token;

    function setUp() public {
        verifier = new JournalVerifier();
        token = new TestToken();
        vault = new PolicyExecutionVault(CUSTOMER, address(verifier), bytes32(uint256(1)), address(token), 100, 150);
        token.mint(address(vault), 1000);
        vm.prank(CUSTOMER);
        vault.setPolicy(POLICY);
        vm.warp(2000);
    }

    function authorization() internal view returns (PolicyExecutionVault.Authorization memory a) {
        a = PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(vault),
            address(token),
            RECIPIENT,
            100,
            bytes32(uint256(1)),
            keccak256("work"),
            1,
            1200,
            4800,
            keccak256("evidence")
        );
    }

    function approve(PolicyExecutionVault.Authorization memory a) internal returns (bytes memory journal) {
        journal = abi.encode(a);
        verifier.approve(journal);
    }

    function reject(PolicyExecutionVault.Authorization memory a) internal {
        bytes memory journal = approve(a);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        require(vault.totalSpent() == 0 && !vault.consumed(a.taskId), "state changed");
    }

    function testPermissionlessPaymentAndReplay() public {
        bytes memory journal = approve(authorization());
        vm.prank(RELAYER);
        vault.pay(hex"abcd", journal);
        require(token.balanceOf(RECIPIENT) == 100 && token.balanceOf(RELAYER) == 0);
        require(vault.totalSpent() == 100 && vault.consumed(bytes32(uint256(1))));
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
    }

    function testAdministrativeAuthorization() public {
        vm.expectRevert();
        vault.setPolicy(POLICY);
        vm.expectRevert();
        vault.setBudget(1000);
        vm.expectRevert();
        vault.cancelTask(bytes32(uint256(1)));
        vm.expectRevert();
        vault.pause();
        vm.expectRevert();
        vault.unpause();
        vm.expectRevert();
        vault.withdraw(1);
        vm.expectRevert();
        vault.transferOwnership(RELAYER);
    }

    function testTwoStepOwnership() public {
        vm.prank(CUSTOMER);
        vault.transferOwnership(RELAYER);
        require(vault.owner() == CUSTOMER);
        vm.expectRevert();
        vault.acceptOwnership();
        vm.prank(RELAYER);
        vault.acceptOwnership();
        require(vault.owner() == RELAYER);
        vm.prank(CUSTOMER);
        vm.expectRevert();
        vault.setBudget(1000);
    }

    function testProofCannotRedirectRecipientOrAmount() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        approve(a);
        a.recipient = RELAYER;
        vm.expectRevert();
        vault.pay(hex"abcd", abi.encode(a));
        a.recipient = RECIPIENT;
        a.amount = 99;
        vm.expectRevert();
        vault.pay(hex"abcd", abi.encode(a));
        require(vault.totalSpent() == 0);
    }

    function testInvalidProof() public {
        bytes memory journal = approve(authorization());
        vm.expectRevert();
        vault.pay(hex"beef", journal);
        require(vault.totalSpent() == 0 && !vault.consumed(bytes32(uint256(1))));
    }

    function testDomains() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        a.chainId += 1;
        reject(a);
        a = authorization();
        a.vault = RELAYER;
        reject(a);
        a = authorization();
        a.token = RELAYER;
        reject(a);
    }

    function testInvalidPaymentFields() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        a.amount = 101;
        reject(a);
        a.amount = 0;
        reject(a);
        a = authorization();
        a.recipient = address(0);
        reject(a);
        a = authorization();
        a.taskId = bytes32(0);
        reject(a);
        a = authorization();
        a.deliverableHash = bytes32(0);
        reject(a);
        a = authorization();
        a.evidenceHash = bytes32(0);
        reject(a);
    }

    function testTimeWindow() public {
        bytes memory journal = approve(authorization());
        vm.warp(1199);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        vm.warp(4801);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        vm.warp(4800);
        vault.pay(hex"abcd", journal);
    }

    function testWindowStartInclusive() public {
        bytes memory journal = approve(authorization());
        vm.warp(1200);
        vault.pay(hex"abcd", journal);
    }

    function testPolicyRotationRevocationAndReplay() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        bytes memory oldJournal = approve(a);
        vm.prank(CUSTOMER);
        vault.setPolicy(POLICY);
        vm.expectRevert();
        vault.pay(hex"abcd", oldJournal);
        a.policyVersion = 2;
        vault.pay(hex"abcd", approve(a));
        vm.prank(CUSTOMER);
        vault.setPolicy(POLICY);
        a.policyVersion = 3;
        bytes memory newer = approve(a);
        vm.expectRevert();
        vault.pay(hex"abcd", newer);
        vm.prank(CUSTOMER);
        vault.setPolicy(bytes32(0));
        a.policyVersion = 4;
        a.taskId = bytes32(uint256(2));
        a.policyHash = bytes32(0);
        newer = approve(a);
        vm.expectRevert();
        vault.pay(hex"abcd", newer);
    }

    function testCancellationAndPause() public {
        bytes memory journal = approve(authorization());
        vm.prank(CUSTOMER);
        vault.pause();
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        vm.prank(CUSTOMER);
        vault.unpause();
        vm.prank(CUSTOMER);
        vault.cancelTask(bytes32(uint256(1)));
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
    }

    function testBudgetAcrossPaymentsAndWithdrawals() public {
        PolicyExecutionVault.Authorization memory a = authorization();
        vault.pay(hex"abcd", approve(a));
        a.taskId = bytes32(uint256(2));
        bytes memory journal = approve(a);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        vm.prank(CUSTOMER);
        vm.expectRevert();
        vault.setBudget(99);
        vm.prank(CUSTOMER);
        vault.withdraw(100);
        token.mint(address(vault), 100);
        require(vault.totalSpent() == 100 && token.balanceOf(CUSTOMER) == 100);
        a.amount = 50;
        vault.pay(hex"abcd", approve(a));
        require(vault.totalSpent() == 150);
    }

    function testTransferFailureRollsBack() public {
        bytes memory journal = approve(authorization());
        token.setFail(true);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
        require(vault.totalSpent() == 0 && !vault.consumed(bytes32(uint256(1))));
        token.setFail(false);
        vault.pay(hex"abcd", journal);
    }

    function testMalformedJournal() public {
        vm.expectRevert();
        vault.pay(hex"abcd", hex"00");
        bytes memory journal = approve(authorization());
        // Set a forbidden high byte in the uint64 chain ID word.
        journal[32] = 0xff;
        verifier.approve(journal);
        vm.expectRevert();
        vault.pay(hex"abcd", journal);
    }

    function testFuzzPaymentWithinCap(uint64 amount) public {
        amount = uint64(uint256(amount) % 100 + 1);
        PolicyExecutionVault.Authorization memory a = authorization();
        a.amount = amount;
        vault.pay(hex"abcd", approve(a));
        require(token.balanceOf(RECIPIENT) == amount && vault.totalSpent() == amount);
    }
}
