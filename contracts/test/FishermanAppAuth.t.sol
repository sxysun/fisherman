// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {FishermanAppAuth} from "../src/FishermanAppAuth.sol";

contract FishermanAppAuthTest is Test {
    FishermanAppAuth auth;
    address owner = address(0xA11CE);
    address other = address(0xB0B);

    function setUp() public {
        auth = new FishermanAppAuth(owner);
    }

    function test_addComposeHash_allowed() public {
        bytes32 h = keccak256("compose-1");
        vm.prank(owner);
        auth.addComposeHash(h);
        assertTrue(auth.isAppAllowed(h));
        assertGt(auth.allowedAt(h), 0);
    }

    function test_addComposeHash_onlyOwner() public {
        bytes32 h = keccak256("compose-1");
        vm.expectRevert(FishermanAppAuth.NotOwner.selector);
        vm.prank(other);
        auth.addComposeHash(h);
    }

    function test_addComposeHash_rejectsZero() public {
        vm.prank(owner);
        vm.expectRevert(FishermanAppAuth.ZeroHash.selector);
        auth.addComposeHash(bytes32(0));
    }

    function test_addComposeHash_rejectsDuplicate() public {
        bytes32 h = keccak256("compose-1");
        vm.startPrank(owner);
        auth.addComposeHash(h);
        vm.expectRevert(FishermanAppAuth.AlreadyAllowed.selector);
        auth.addComposeHash(h);
    }

    function test_revoke() public {
        bytes32 h = keccak256("compose-1");
        vm.startPrank(owner);
        auth.addComposeHash(h);
        auth.revokeComposeHash(h);
        assertFalse(auth.isAppAllowed(h));
    }

    function test_revoke_onlyAllowed() public {
        bytes32 h = keccak256("never-added");
        vm.prank(owner);
        vm.expectRevert(FishermanAppAuth.NotAllowed.selector);
        auth.revokeComposeHash(h);
    }

    function test_ownerTransfer_twoStep() public {
        vm.prank(owner);
        auth.proposeOwner(other);
        assertEq(auth.pendingOwner(), other);
        vm.prank(other);
        auth.acceptOwner();
        assertEq(auth.owner(), other);
        assertEq(auth.pendingOwner(), address(0));
    }

    function test_ownerTransfer_pendingOnly() public {
        vm.prank(owner);
        auth.proposeOwner(other);
        vm.expectRevert(FishermanAppAuth.NotPending.selector);
        vm.prank(address(0xCAFE));
        auth.acceptOwner();
    }
}
