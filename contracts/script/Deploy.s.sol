// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import {FishermanAppAuth} from "../src/FishermanAppAuth.sol";

contract Deploy is Script {
    function run() external {
        uint256 pk = vm.envUint("OWNER_PK");
        address owner = vm.envOr("OWNER_ADDR", vm.addr(pk));
        vm.startBroadcast(pk);
        FishermanAppAuth auth = new FishermanAppAuth(owner);
        console2.log("FishermanAppAuth deployed at", address(auth));
        console2.log("owner =", owner);
        vm.stopBroadcast();
    }
}
