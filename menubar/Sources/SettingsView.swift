import SwiftUI

struct SettingsView: View {
    var config: ConfigManager
    var onSave: () -> Void
    var onCancel: () -> Void

    @State private var serverURL: String = ""
    @State private var controlPort: String = ""
    @State private var selectedTab: SettingsTab = .server

    @State private var displayName: String = ""

    // Friend code paste
    @State private var friendCodeInput: String = ""
    @State private var parsedFriendCode: FriendCode?
    @State private var friendCodeOverrideName: String = ""
    @State private var friendCodeError: String?

    // Manual add friend form (power user toggle)
    @State private var newFriendName: String = ""
    @State private var newFriendPubkey: String = ""
    @State private var newFriendServer: String = ""
    @State private var newFriendPort: String = "9998"
    @State private var showAddFriend = false
    @State private var showManualAdd = false
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
                    config.displayName = displayName
                    config.save()
                    onSave()
                }
                .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .frame(width: 420, height: 480)
        .onAppear {
            serverURL = config.serverURL
            controlPort = config.controlPort
            displayName = config.displayName
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
            sectionHeader("Your Friend Code")

            hintText("Share this code with friends. They paste it to add you.")

            fieldRow("Your Name", placeholder: "alice", text: $displayName)

            // Friend code display
            if let code = config.generateFriendCode(name: displayName) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Friend Code")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        copyButton(code, label: "Copy")
                    }
                    monoBox(code)
                }
            } else {
                hintText("Configure your server URL first to generate a friend code.")
            }

            Divider()

            sectionHeader("Key Pair")

            hintText("Generated automatically on first launch.")

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

            hintText("Add FISH_PRIVATE_KEY to your server's .env:\nFISH_PRIVATE_KEY=\(config.privateKeyHex.prefix(8))...")
        }
    }

    // MARK: - Friends tab

    private var friendsTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Friends")

            hintText("Paste a friend code to add someone. Both sides must add each other.")

            // Friend code paste field
            VStack(alignment: .leading, spacing: 6) {
                Text("Paste friend code")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                TextField("fish:eyJ...", text: $friendCodeInput)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12, design: .monospaced))
                    .onChange(of: friendCodeInput) { _, newValue in
                        parseFriendCodeInput(newValue)
                    }
            }

            // Preview card when valid
            if let code = parsedFriendCode {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(code.n)
                                .font(.system(size: 12, weight: .semibold))
                            Text("\(code.h):\(code.w)")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                            Text(code.k.prefix(16) + "...")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                        }
                        Spacer()
                    }

                    fieldRow("Display as", placeholder: code.n, text: $friendCodeOverrideName)

                    if let error = friendCodeError {
                        Text(error)
                            .font(.system(size: 11))
                            .foregroundStyle(.red)
                    }

                    Button("Add") {
                        addFriendFromCode()
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                }
                .padding(10)
                .background(Color.secondary.opacity(0.05))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else if !friendCodeInput.isEmpty {
                if let error = friendCodeError {
                    Text(error)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                }
            }

            Divider()

            // Existing friends list
            if config.friends.isEmpty {
                HStack {
                    Spacer()
                    VStack(spacing: 6) {
                        Text("No friends yet")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        Text("Paste a friend code above to get started")
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

            // Manual add toggle for power users
            if showManualAdd {
                addFriendForm
            } else {
                Button {
                    showManualAdd = true
                } label: {
                    Text("Or add manually...")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Add friend form

    private var addFriendForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Add Manually")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                Button("Cancel") {
                    showManualAdd = false
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

    private func parseFriendCodeInput(_ input: String) {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            parsedFriendCode = nil
            friendCodeError = nil
            friendCodeOverrideName = ""
            return
        }

        if let code = ConfigManager.parseFriendCode(trimmed) {
            // Validate pubkey length
            guard code.k.count == 64, code.k.allSatisfy({ $0.isHexDigit }) else {
                parsedFriendCode = nil
                friendCodeError = "Invalid public key in code"
                return
            }
            parsedFriendCode = code
            friendCodeOverrideName = code.n
            friendCodeError = nil
        } else {
            parsedFriendCode = nil
            if trimmed.hasPrefix("fish:") {
                friendCodeError = "Invalid friend code"
            } else {
                friendCodeError = "Code must start with fish:"
            }
        }
    }

    private func addFriendFromCode() {
        guard let code = parsedFriendCode else { return }

        // Check for duplicate
        if config.friends.contains(where: { $0.publicKey == code.k }) {
            friendCodeError = "Already added this friend"
            return
        }

        let name = friendCodeOverrideName.trimmingCharacters(in: .whitespaces)
        config.addFriendFromCode(code, overrideName: name.isEmpty ? nil : name)

        // Reset
        friendCodeInput = ""
        parsedFriendCode = nil
        friendCodeOverrideName = ""
        friendCodeError = nil
    }

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
        config.registerFriendOnServer(pubkey: pubkey)
        showManualAdd = false
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
