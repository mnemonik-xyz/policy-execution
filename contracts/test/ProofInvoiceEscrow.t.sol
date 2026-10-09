// SPDX-License-Identifier: LicenseRef-Mnemonik-SRL-1.0
pragma solidity ^0.8.24;

import {ProofInvoiceEscrow} from "../src/ProofInvoiceEscrow.sol";
import {ProofInvoiceFactory} from "../src/ProofInvoiceFactory.sol";
import {InvoiceEscrow} from "../src/InvoiceEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";
import {InvoiceVm} from "./InvoiceEscrow.t.sol";

/// @dev Halmos checks the check_* properties over symbolic arguments. The verifier
/// is an explicit cryptographic abstraction; this does not prove RISC Zero soundness.
contract ProofInvoiceEscrowTest {
    InvoiceVm constant vm = InvoiceVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant VENDOR = address(0x1234);
    bytes32 constant POLICY = bytes32(uint256(11));
    bytes32 constant PO = bytes32(uint256(22));
    TestToken token;
    JournalVerifier verifier;
    ProofInvoiceEscrow escrow;
    ProofInvoiceFactory factory;
    bytes32 id;

    function setUp() public {
        vm.warp(1000);
        token = new TestToken();
        verifier = new JournalVerifier();
        factory = new ProofInvoiceFactory(address(token), address(verifier));
        escrow = factory.createEscrow(bytes32(uint256(1)));
        token.mint(address(this), 10000);
        token.approve(address(escrow), 10000);
        id = escrow.offer(InvoiceEscrow.Terms(POLICY, 1, PO, VENDOR, 3000, address(0), 0, 0, 1200, 5000));
        vm.prank(VENDOR);
        escrow.accept(id);
    }

    function authorization(uint64 amount, bytes32 obligation)
        internal
        view
        returns (PolicyExecutionVault.Authorization memory)
    {
        return PolicyExecutionVault.Authorization(
            POLICY,
            uint64(block.chainid),
            address(escrow),
            address(token),
            VENDOR,
            amount,
            obligation,
            bytes32(uint256(33)),
            1,
            1000,
            5000,
            bytes32(uint256(44))
        );
    }

    function journal(uint64 amount, bytes32 obligation) internal view returns (bytes memory) {
        return abi.encode(authorization(amount, obligation), PO, uint64(3000), address(this));
    }

    function attempt(bytes memory j) internal returns (bool ok) {
        (ok,) = address(escrow).call(abi.encodeCall(escrow.settle, (hex"abcd", j)));
    }

    function check_proofRequired(uint64 amount, bool proven) public {
        bytes memory j = journal(amount, bytes32(uint256(1)));
        if (proven) verifier.approve(j);
        bool ok = attempt(j);
        assert(ok == (proven && amount > 0 && amount <= 3000));
        if (!ok) {
            assert(escrow.order(id).spent == 0);
            assert(!escrow.consumed(address(this), bytes32(uint256(1))));
            assert(token.balanceOf(VENDOR) == 0);
        }
    }

    function check_exactPaymentAndReplay(uint64 amount) public {
        vm.assume(amount > 0 && amount <= 3000);
        bytes memory j = journal(amount, bytes32(uint256(1)));
        verifier.approve(j);
        assert(attempt(j)); // Assert success explicitly: reverting paths must not prove vacuously.
        assert(token.balanceOf(VENDOR) == amount);
        assert(token.balanceOf(address(escrow)) == 3000 - amount);
        assert(escrow.order(id).spent == amount);
        assert(escrow.totalReserved() == 3000 - amount);
        assert(escrow.consumed(address(this), bytes32(uint256(1))));
        assert(!attempt(j));
        assert(token.balanceOf(VENDOR) == amount);
    }

    function check_noBypass(bytes32 obligation, uint64 amount) public {
        bytes memory j = journal(amount, obligation);
        verifier.approve(j);
        (bool signedOk,) = address(escrow).call(abi.encodeCall(escrow.settleSigned, (j, hex"")));
        (bool approvedOk,) =
            address(escrow).call(abi.encodeCall(escrow.settleApproved, (id, obligation, amount, bytes32(uint256(33)))));
        assert(!signedOk && !approvedOk);
        assert(token.balanceOf(VENDOR) == 0 && escrow.order(id).spent == 0);
    }

    function check_ceilingAcrossPayments(uint64 first, uint64 second) public {
        vm.assume(first > 0 && first <= 3000);
        bytes memory j = journal(first, bytes32(uint256(1)));
        verifier.approve(j);
        assert(attempt(j));
        j = journal(second, bytes32(uint256(2)));
        verifier.approve(j);
        bool ok = attempt(j);
        assert(ok == (second > 0 && second <= 3000 - first));
        uint256 paid = uint256(first) + (ok ? second : 0);
        assert(token.balanceOf(VENDOR) == paid);
        assert(escrow.totalReserved() == 3000 - paid);
    }

    function check_failedTransferIsAtomic(uint64 amount) public {
        vm.assume(amount > 0 && amount <= 3000);
        bytes memory j = journal(amount, bytes32(uint256(1)));
        verifier.approve(j);
        token.setFail(true);
        assert(!attempt(j));
        assert(escrow.order(id).spent == 0 && escrow.totalReserved() == 3000);
        assert(!escrow.consumed(address(this), bytes32(uint256(1))));
        assert(!factory.consumed(address(this), bytes32(uint256(1))));
        token.setFail(false);
        assert(attempt(j));
    }

    function check_domainBinding(
        uint64 chain,
        address vault,
        address asset,
        address recipient,
        bytes32 policy,
        uint64 version
    ) public {
        PolicyExecutionVault.Authorization memory a = authorization(100, bytes32(uint256(1)));
        a.chainId = chain;
        a.vault = vault;
        a.token = asset;
        a.recipient = recipient;
        a.policyHash = policy;
        a.policyVersion = version;
        bytes memory j = abi.encode(a, PO, uint64(3000), address(this));
        verifier.approve(j);
        bool ok = attempt(j);
        if (ok) {
            assert(
                chain == block.chainid && vault == address(escrow) && asset == address(token) && recipient == VENDOR
                    && policy == POLICY && version == 1
            );
        }
    }

    function check_closePreventsSettlement(uint64 amount) public {
        vm.assume(amount > 0 && amount <= 3000);
        bytes memory j = journal(amount, bytes32(uint256(1)));
        verifier.approve(j);
        vm.warp(5001);
        escrow.close(id);
        assert(token.balanceOf(address(this)) == 10000);
        assert(escrow.totalReserved() == 0);
        assert(!attempt(j));
    }

    function testFuzzProofRequired(uint64 amount, bool proven) public {
        check_proofRequired(amount, proven);
    }

    function testFuzzNoBypass(bytes32 obligation, uint64 amount) public {
        check_noBypass(obligation, amount);
    }

    function testFuzzAccounting(uint16 first, uint16 second) public {
        check_ceilingAcrossPayments(uint64(first % 3000) + 1, second);
    }

    function testPaymentAndReplay() public {
        check_exactPaymentAndReplay(100);
    }

    function testTransferFailureIsAtomic() public {
        check_failedTransferIsAtomic(100);
    }

    function testClosePreventsSettlement() public {
        check_closePreventsSettlement(100);
    }

    function testCannotConfigureSigner() public {
        vm.expectRevert(InvoiceEscrow.ProofRequired.selector);
        escrow.offer(
            InvoiceEscrow.Terms(POLICY, 1, bytes32(uint256(23)), VENDOR, 3000, address(0x5555), 100, 100, 1200, 5000)
        );
    }

    function check_registryCannotBePoisoned(address customer, bytes32 obligation) public {
        (bool ok,) = address(factory).call(abi.encodeCall(factory.consume, (customer, obligation)));
        assert(!ok);
        assert(!factory.consumed(customer, obligation));
    }

    function testReplayAcrossEscrows() public {
        bytes32 obligation = bytes32(uint256(1));
        bytes memory j = journal(100, obligation);
        verifier.approve(j);
        assert(attempt(j));
        ProofInvoiceEscrow next = factory.createEscrow(bytes32(uint256(1)));
        token.approve(address(next), 3000);
        bytes32 nextId = next.offer(InvoiceEscrow.Terms(POLICY, 1, PO, VENDOR, 3000, address(0), 0, 0, 1200, 5000));
        vm.prank(VENDOR);
        next.accept(nextId);
        PolicyExecutionVault.Authorization memory a = authorization(100, obligation);
        a.vault = address(next);
        j = abi.encode(a, PO, uint64(3000), address(this));
        verifier.approve(j);
        vm.expectRevert(ProofInvoiceFactory.AlreadyConsumed.selector);
        next.settle(hex"abcd", j);
        assert(next.order(nextId).spent == 0);
        assert(token.balanceOf(VENDOR) == 100);
    }
}
