// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Local showcase currency. Never use this unrestricted mint on a public network.
contract ShowcaseToken is ERC20 {
    constructor() ERC20("Showcase USD", "sUSD") {}

    function decimals() public pure override returns (uint8) { return 6; }

    function mint(address recipient, uint256 amount) external {
        require(block.chainid == 31337, "local chain only");
        _mint(recipient, amount);
    }
}
