// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FishermanAppAuth
/// @notice Allow-list of compose_hashes for fisherman-mirror TDX
///         deployments. The menubar verifies that the live mirror's
///         compose_hash is `isAppAllowed()` before pairing. Older hashes
///         stay allowed so older client builds continue to work.
///
/// Same shape as feedling-mcp-v1's FeedlingAppAuth.
contract FishermanAppAuth {
    address public owner;
    address public pendingOwner;

    /// @dev compose_hash → block number it was added at (0 = not allowed)
    mapping(bytes32 => uint256) public allowedAt;

    event ComposeHashAdded(bytes32 indexed composeHash, uint256 blockNumber);
    event ComposeHashRevoked(bytes32 indexed composeHash);
    event OwnerProposed(address indexed pending);
    event OwnerAccepted(address indexed prev, address indexed next);

    error NotOwner();
    error NotPending();
    error ZeroHash();
    error AlreadyAllowed();
    error NotAllowed();

    constructor(address _owner) {
        owner = _owner;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @notice Allow a new compose_hash. Existing entries are kept.
    function addComposeHash(bytes32 composeHash) external onlyOwner {
        if (composeHash == bytes32(0)) revert ZeroHash();
        if (allowedAt[composeHash] != 0) revert AlreadyAllowed();
        allowedAt[composeHash] = block.number;
        emit ComposeHashAdded(composeHash, block.number);
    }

    /// @notice Revoke a previously-allowed hash. Use sparingly — older
    ///         clients that pin against this hash will refuse to pair.
    function revokeComposeHash(bytes32 composeHash) external onlyOwner {
        if (allowedAt[composeHash] == 0) revert NotAllowed();
        delete allowedAt[composeHash];
        emit ComposeHashRevoked(composeHash);
    }

    /// @notice True if the compose_hash is currently allowed.
    function isAppAllowed(bytes32 composeHash) external view returns (bool) {
        return allowedAt[composeHash] != 0;
    }

    /// @notice Two-step ownership transfer.
    function proposeOwner(address next) external onlyOwner {
        pendingOwner = next;
        emit OwnerProposed(next);
    }

    function acceptOwner() external {
        if (msg.sender != pendingOwner) revert NotPending();
        address prev = owner;
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnerAccepted(prev, msg.sender);
    }
}
