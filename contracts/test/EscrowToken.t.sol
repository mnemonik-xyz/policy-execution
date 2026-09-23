// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
import {TaskEscrow} from "../src/TaskEscrow.sol";
import {PolicyExecutionVault} from "../src/PolicyExecutionVault.sol";
import {Vm, JournalVerifier, TestToken} from "./PolicyExecutionVault.t.sol";

contract CallbackToken is TestToken {
    address public target;
    bytes public callback;
    bool public callbackSucceeded;
    bool public fee;

    function configure(address target_, bytes calldata callback_, bool fee_) external {
        target = target_;
        callback = callback_;
        fee = fee_;
    }

    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        bool ok = super.transferFrom(from, to, amount);
        if (fee) _burn(to, 1);
        return ok;
    }

    function transfer(address to, uint256 amount) public override returns (bool) {
        if (target != address(0)) (callbackSucceeded,) = target.call(callback);
        return super.transfer(to, amount);
    }
}

contract EscrowTokenTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function testFeeFundingCannotBorrowOtherReservation() public {
        vm.warp(1000);
        CallbackToken token = new CallbackToken();
        JournalVerifier verifier = new JournalVerifier();
        TaskEscrow escrow = new TaskEscrow(address(token), address(verifier), bytes32(uint256(1)));
        token.mint(address(this), 200);
        token.approve(address(escrow), 200);
        escrow.offer(bytes32(uint256(1)), address(1), bytes32(uint256(1)), 1, 100, 1200, 2000);
        token.configure(address(0), hex"", true);
        vm.expectRevert();
        escrow.offer(bytes32(uint256(2)), address(1), bytes32(uint256(1)), 1, 100, 1200, 2000);
        require(escrow.totalReserved() == 100 && token.balanceOf(address(escrow)) == 100);
    }

    function testTokenCallbackCannotReenterSettlement() public {
        vm.warp(1000);
        CallbackToken token = new CallbackToken();
        JournalVerifier verifier = new JournalVerifier();
        TaskEscrow escrow = new TaskEscrow(address(token), address(verifier), bytes32(uint256(1)));
        token.mint(address(this), 100);
        token.approve(address(escrow), 100);
        bytes32 task = escrow.offer(bytes32(uint256(1)), address(this), bytes32(uint256(1)), 1, 100, 1200, 2000);
        escrow.accept(task);
        PolicyExecutionVault.Authorization memory a = PolicyExecutionVault.Authorization(
            bytes32(uint256(1)),
            uint64(block.chainid),
            address(escrow),
            address(token),
            address(this),
            100,
            task,
            bytes32(uint256(2)),
            1,
            1000,
            2000,
            bytes32(uint256(3))
        );
        bytes memory journal = abi.encode(a);
        verifier.approve(journal);
        token.configure(address(escrow), abi.encodeCall(escrow.settle, (hex"abcd", journal)), false);
        escrow.settle(hex"abcd", journal);
        require(!token.callbackSucceeded() && escrow.totalReserved() == 0 && token.balanceOf(address(this)) == 100);
    }
}
