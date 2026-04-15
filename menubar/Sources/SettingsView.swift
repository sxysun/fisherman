import SwiftUI

struct SettingsView: View {
    var config: ConfigManager
    var onSave: () -> Void
    var onCancel: () -> Void

    @State private var serverURL: String = ""
    @State private var controlPort: String = ""
    @State private var selectedTab: SettingsTab = .server

    // Add friend form
    @State private var newFriendName: String = ""
    @State private var newFriendPubkey: String = ""
    @State private var newFriendServer: String = ""
    @State private var newFriendPort: String = "9998"
    @State private var showAddFriend = false
    @State private var addFriendError: String?

    enum SettingsTab: String, CaseIterable {
        case server = "Server"
        case identity = "Identity"
        case friends = "Friends"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Tab bar
            HStack(spacing: 0) {
                ForEach(SettingsTab.allCases, id: \.self) { tab in
                    Button {
                        selectedTab = tab
                    } label: {
                        Text(tab.rawValue)
                            .font(.system(size: 12, weight: selectedTab == tab ? .semibold : .regular))
                            .foregroundStyle(selectedTab == tab ? .primary : .secondary)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(selectedTab == tab ? Color.accentColor.opacity(0.1) : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                }
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)

            Divider().padding(.top, 8)

            // Tab content
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    switch selectedTab {
                    case .server:
                        serverTab
                    case .identity:
                        identityTab
                    case .friends:
                        friendsTab
                    }
                }
                .padding(16)
            }

            Divider()

            // Bottom buttons
            HStack {
                Text("Saved to ~/.fisherman/.env")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("Cancel") { onCancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Save") {
                    config.serverURL = serverURL
                    config.controlPort = controlPort
                    config.save()
                    onSave()
                }
                .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .frame(width: 400, height: 420)
        .onAppear {
            serverURL = config.serverURL
            controlPort = config.controlPort
        }
    }

    // MARK: - Server tab

    private var serverTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Connection")

            fieldRow("Server URL", placeholder: "ws://your-server:9999/ingest", text: $serverURL)
            fieldRow("Control Port", placeholder: "7892", text: $controlPort)

            hintText("Your server runs ingest.py. Set FISH_PRIVATE_KEY in the server's .env to the same private key shown in the Identity tab.")
        }
    }

    // MARK: - Identity tab

    private var identityTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Your Key Pair")

            hintText("Your identity is your key pair. It was generated automatically on first launch.")

            // Public key (the one you share)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Public Key")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    copyButton(config.publicKeyHex, label: "Copy")
                }
                monoBox(config.publicKeyHex)
            }

            hintText("Share your public key with friends so they can add you.")

            Divider()

            // Private key (keep secret, copy to server)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Private Key")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    copyButton(config.privateKeyHex, label: "Copy")
                }
                monoBox(String(config.privateKeyHex.prefix(16)) + "..." + String(config.privateKeyHex.suffix(8)))
            }

            hintText("Add this to your server's .env file as:\nFISH_PRIVATE_KEY=\(config.privateKeyHex.prefix(8))...")

            Divider()

            sectionHeader("Server Setup")

            VStack(alignment: .leading, spacing: 6) {
                Text("On your server, add to .env:")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)

                let envSnippet = "FISH_PRIVATE_KEY=\(config.privateKeyHex)"
                HStack {
                    monoBox(String(envSnippet.prefix(40)) + "...")
                    copyButton(envSnippet, label: "Copy line")
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("To let friends query your activity, add their public keys:")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)

                let friendKeys = config.friends.map(\.publicKey).joined(separator: ",")
                let fishFriendsLine = "FISH_FRIENDS=\(friendKeys.isEmpty ? "<pubkey1>,<pubkey2>" : friendKeys)"
                HStack {
                    monoBox(String(fishFriendsLine.prefix(48)) + (fishFriendsLine.count > 48 ? "..." : ""))
                    copyButton(fishFriendsLine, label: "Copy line")
                }
            }
        }
    }

    // MARK: - Friends tab

    private var friendsTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Friends")

            hintText("Add friends to see their activity in your notch. They must also add your public key to their server's FISH_FRIENDS.")

            if config.friends.isEmpty {
                HStack {
                    Spacer()
                    VStack(spacing: 6) {
                        Text("No friends yet")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        Text("Add a friend to see their activity")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                }
                .padding(.vertical, 12)
            } else {
                ForEach(Array(config.friends.enumerated()), id: \.offset) { index, friend in
                    friendRow(friend, index: index)
                }
            }

            Divider()

            if showAddFriend {
                addFriendForm
            } else {
                Button {
                    showAddFriend = true
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "plus.circle.fill")
                            .font(.system(size: 12))
                        Text("Add Friend")
                            .font(.system(size: 12, weight: .medium))
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
    }

    // MARK: - Add friend form

    private var addFriendForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Add Friend")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                Button("Cancel") {
                    showAddFriend = false
                    clearAddFriendForm()
                }
                .font(.system(size: 11))
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
            }

            fieldRow("Name", placeholder: "alice", text: $newFriendName)
            fieldRow("Public Key", placeholder: "64-char hex from their Identity tab", text: $newFriendPubkey)
            fieldRow("Server URL", placeholder: "ws://their-server:9999", text: $newFriendServer)
            fieldRow("Activity Port", placeholder: "9998", text: $newFriendPort)

            if let error = addFriendError {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
            }

            Button("Add") {
                addFriend()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
            .disabled(newFriendName.isEmpty || newFriendPubkey.isEmpty || newFriendServer.isEmpty)
        }
        .padding(10)
        .background(Color.secondary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // MARK: - Reusable components

    private func friendRow(_ friend: Friend, index: Int) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(friend.name)
                    .font(.system(size: 12, weight: .medium))
                Text("\(friend.serverURL) : \(friend.activityPort)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                Text(friend.publicKey.prefix(16) + "...")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            Button {
                config.removeFriend(at: index)
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: 11))
                    .foregroundStyle(.red.opacity(0.7))
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 4)
    }

    private func fieldRow(_ label: String, placeholder: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            TextField(placeholder, text: text)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12, design: .monospaced))
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.system(size: 13, weight: .semibold))
    }

    private func hintText(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func monoBox(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 11, design: .monospaced))
            .lineLimit(1)
            .truncationMode(.middle)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 5))
    }

    private func copyButton(_ text: String, label: String) -> some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: "doc.on.doc")
                    .font(.system(size: 10))
                Text(label)
                    .font(.system(size: 10))
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.mini)
    }

    // MARK: - Actions

    private func addFriend() {
        addFriendError = nil

        let name = newFriendName.trimmingCharacters(in: .whitespaces)
        let pubkey = newFriendPubkey.trimmingCharacters(in: .whitespaces)
        let server = newFriendServer.trimmingCharacters(in: .whitespaces)
        let port = newFriendPort.trimmingCharacters(in: .whitespaces)

        guard !name.isEmpty else { addFriendError = "Name is required"; return }
        guard pubkey.count == 64, pubkey.allSatisfy({ $0.isHexDigit }) else {
            addFriendError = "Public key must be 64 hex characters"
            return
        }
        guard server.hasPrefix("ws://") || server.hasPrefix("wss://") else {
            addFriendError = "Server URL must start with ws:// or wss://"
            return
        }

        config.addFriend(name: name, publicKey: pubkey, serverURL: server, activityPort: port.isEmpty ? "9998" : port)
        showAddFriend = false
        clearAddFriendForm()
    }

    private func clearAddFriendForm() {
        newFriendName = ""
        newFriendPubkey = ""
        newFriendServer = ""
        newFriendPort = "9998"
        addFriendError = nil
    }
}
