import SwiftUI

struct SettingsView: View {
    var config: ConfigManager
    var onSave: () -> Void
    var onCancel: () -> Void

    @State private var serverURL: String = ""
    @State private var backendMode: String = "local"
    @State private var selfHostedURL: String = ""
    @State private var cloudURL: String = ""
    @State private var statusRelayURL: String = ""
    @State private var controlPort: String = ""
    @State private var showAdvancedBackend = false
    @State private var selectedTab: SettingsTab = .server
    @State private var cloudApprovalStatus: String?
    @State private var approvingCloud = false

    @State private var displayName: String = ""

    // Friend code paste
    @State private var friendCodeInput: String = ""
    @State private var parsedFriendCode: FriendCode?
    @State private var friendCodeOverrideName: String = ""
    @State private var friendCodeError: String?

    @State private var editingRelayFriendPubkey: String?
    @State private var editingFriendAudience: String = "friends"
    @State private var editingFriendPolicyPrompt: String = ""
    @State private var friendPolicyError: String?

    private let defaultCloudURL = "https://fisherman.teleport.computer"
    private let defaultServerURL = "ws://localhost:9999/ingest"
    private let friendAudiences = ["friends", "work", "close", "custom"]

    enum SettingsTab: String, CaseIterable {
        case server = "Backend"
        case identity = "Identity"
        case friends = "Friends"
        case deputies = "Agent Access"
        case storage = "Backup"
        case agent = "Agent"
        case diagnostics = "Diagnostics"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Tab bar — horizontally scrollable so adding a new tab
            // doesn't force the whole row to word-wrap when the window
            // is at its default width.
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(SettingsTab.allCases, id: \.self) { tab in
                        Button {
                            selectedTab = tab
                        } label: {
                            Text(tab.rawValue)
                                .font(.system(size: 12, weight: selectedTab == tab ? .semibold : .regular))
                                .foregroundStyle(selectedTab == tab ? .primary : .secondary)
                                .lineLimit(1)
                                .fixedSize(horizontal: true, vertical: false)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 8)
                                .background(selectedTab == tab ? Color.accentColor.opacity(0.12) : Color.clear)
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
            }
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
                    case .deputies:
                        DeputiesTab()
                    case .storage:
                        BackupTab()
                    case .agent:
                        AgentTab()
                    case .diagnostics:
                        DiagnosticsTab()
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
                    let trimmedSelfHostedURL = selfHostedURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    let trimmedCloudURL = cloudURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    let previousBackendMode = config.backendMode
                    let previousBackendURL = config.backendURL.isEmpty ? defaultCloudURL : config.backendURL
                    let previousServerURL = config.serverURL
                    config.backendMode = backendMode
                    if backendMode == "cloud" {
                        let savedCloudURL = trimmedCloudURL.isEmpty ? defaultCloudURL : trimmedCloudURL
                        let expectedIngestURL = ingestURL(from: savedCloudURL)
                        let previousExpectedIngestURL = ingestURL(from: previousBackendURL)
                        let hadApprovedCloudIngest =
                            previousBackendMode == "cloud" &&
                            savedCloudURL == previousBackendURL &&
                            previousServerURL == previousExpectedIngestURL
                        config.backendURL = savedCloudURL
                        config.serverURL = savedCloudURL.hasPrefix("ws://") || savedCloudURL.hasPrefix("wss://")
                            ? ingestURL(from: savedCloudURL)
                            : (hadApprovedCloudIngest ? expectedIngestURL : defaultServerURL)
                    } else if backendMode == "self_hosted" {
                        config.backendURL = trimmedSelfHostedURL
                        config.serverURL = ingestURL(from: trimmedSelfHostedURL)
                    } else {
                        config.backendURL = ""
                        config.serverURL = defaultServerURL
                    }
                    config.statusRelayURL = statusRelayURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    config.controlPort = controlPort.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "7892" : controlPort
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
            backendMode = config.backendMode
            cloudURL = config.backendMode == "cloud" && !config.backendURL.isEmpty ? config.backendURL : defaultCloudURL
            selfHostedURL = config.backendMode == "self_hosted" ? config.backendURL : ""
            statusRelayURL = config.statusRelayURL
            serverURL = config.serverURL
            controlPort = config.controlPort
            displayName = config.displayName
            config.refreshRelayFriends()
        }
    }

    // MARK: - Server tab

    private var serverTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Backend")

            Picker("Mode", selection: $backendMode) {
                Text("Local Only").tag("local")
                Text("Fisherman Cloud").tag("cloud")
                Text("Self-hosted").tag("self_hosted")
            }
            .pickerStyle(.segmented)

            if backendMode == "cloud" {
                hintText("Managed by Fisherman. Service endpoints and encrypted friend-status relay are configured automatically.")
                hintText("Raw context is processed inside the attested Cloud CVM and encrypted at rest with a CVM-held key. This is not client-held end-to-end encryption from the Fisherman operator yet.")
                hintText("Friend status uses separate end-to-end encryption to each friend; the relay does not receive plaintext status.")
                HStack(spacing: 8) {
                    Button(approvingCloud ? "Reviewing..." : "Review & Approve Cloud") {
                        approveCloud()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(approvingCloud)
                    if let cloudApprovalStatus {
                        Text(cloudApprovalStatus)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
            } else if backendMode == "self_hosted" {
                fieldRow("Self-hosted URL", placeholder: "wss://your-server:9999/ingest", text: $selfHostedURL)
                hintText("Use this when you run your own Fisherman server. One URL is enough; Fisherman derives the activity and history endpoints automatically.")
            } else {
                hintText("Raw context stays on this laptop. Friend status can still use the encrypted relay when configured.")
            }

            DisclosureGroup("Advanced endpoints", isExpanded: $showAdvancedBackend) {
                VStack(alignment: .leading, spacing: 10) {
                    if backendMode == "cloud" {
                        fieldRow("Fisherman Cloud URL", placeholder: defaultCloudURL, text: $cloudURL)
                    }
                    fieldRow("Status relay", placeholder: "https://relay.fisherman.teleport.computer", text: $statusRelayURL)
                    if backendMode != "cloud" {
                        fieldRow("Local control port", placeholder: "7892", text: $controlPort)
                    }
                    hintText(backendMode == "cloud"
                             ? "Only change these for Fisherman Cloud development or a custom relay."
                             : "Only change these for local development or custom relays.")
                }
                .padding(.top, 8)
            }
            .font(.system(size: 12, weight: .medium))

            hintText("Backup and agent access are optional capabilities. They are not required for Self-hosted or Fisherman Cloud.")
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
                hintText("Identity key not ready yet. Fisherman generates it automatically on first launch.")
            }

            Divider()

            sectionHeader("Key Pair")

            hintText("Generated automatically on first launch.")

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Signing Public Key")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    copyButton(config.publicKeyHex, label: "Copy")
                }
                monoBox(config.publicKeyHex)
            }

            if let encryptionPubkey = config.encryptionPublicKeyHex() {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Encryption Public Key")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        copyButton(encryptionPubkey, label: "Copy")
                    }
                    monoBox(encryptionPubkey)
                }
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
                            if let relay = code.r {
                                Text("relay: \(relay)")
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            } else {
                                Text("default relay")
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                            Text(code.k.prefix(16) + "...")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                            Text("enc \(code.x.prefix(16))...")
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
            if config.relayFriends.isEmpty {
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
                ForEach(config.relayFriends, id: \.pubkeyHex) { friend in
                    relayFriendRow(friend)
                }
            }
        }
    }

    // MARK: - Reusable components

    private func relayFriendRow(_ friend: RelayFriend) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        Text(friend.name)
                            .font(.system(size: 12, weight: .medium))
                        Text("Relay")
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Color.secondary.opacity(0.08))
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                        Text(friend.audience.capitalized)
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.secondary)
                    }
                    Text(friend.relayURL ?? "default relay")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(friend.pubkeyHex.prefix(16) + "...")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    Text("enc \(friend.encryptionPubkeyHex.prefix(16))...")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                    if let prompt = friend.policyPrompt, !prompt.isEmpty {
                        Text(prompt)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                Spacer()

                Button {
                    beginEditingRelayFriend(friend)
                } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 11))
                }
                .buttonStyle(.plain)
                .help("Edit sharing policy")

                Button {
                    let result = CliBridge.run(["friend", "remove", friend.pubkeyHex])
                    if result.exitCode == 0 {
                        if editingRelayFriendPubkey == friend.pubkeyHex {
                            cancelEditingRelayFriend()
                        }
                        config.refreshRelayFriends()
                    }
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 11))
                        .foregroundStyle(.red.opacity(0.7))
                }
                .buttonStyle(.plain)
            }

            if editingRelayFriendPubkey == friend.pubkeyHex {
                relayFriendPolicyEditor(friend)
            }
        }
        .padding(.vertical, 4)
    }

    private func relayFriendPolicyEditor(_ friend: RelayFriend) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Audience")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Picker("", selection: $editingFriendAudience) {
                    ForEach(friendAudiences, id: \.self) { audience in
                        Text(audience.capitalized).tag(audience)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Custom instruction")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                TextEditor(text: $editingFriendPolicyPrompt)
                    .font(.system(size: 12))
                    .frame(minHeight: 58)
                    .scrollContentBackground(.hidden)
                    .background(Color.secondary.opacity(0.06))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                hintText("Optional. Used by the status agent after hard privacy filters.")
            }

            if let error = friendPolicyError {
                Text(error)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                Button("Cancel") {
                    cancelEditingRelayFriend()
                }
                .controlSize(.small)
                Button("Save Policy") {
                    saveRelayFriendPolicy(friend)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 8))
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

    private func beginEditingRelayFriend(_ friend: RelayFriend) {
        editingRelayFriendPubkey = friend.pubkeyHex
        editingFriendAudience = friendAudiences.contains(friend.audience) ? friend.audience : "friends"
        editingFriendPolicyPrompt = friend.policyPrompt ?? ""
        friendPolicyError = nil
    }

    private func cancelEditingRelayFriend() {
        editingRelayFriendPubkey = nil
        editingFriendAudience = "friends"
        editingFriendPolicyPrompt = ""
        friendPolicyError = nil
    }

    private func saveRelayFriendPolicy(_ friend: RelayFriend) {
        friendPolicyError = nil
        let prompt = editingFriendPolicyPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        var args = [
            "friend", "policy", friend.pubkeyHex,
            "--audience", editingFriendAudience,
        ]
        if prompt.isEmpty {
            args.append("--clear-policy-prompt")
        } else {
            args += ["--policy-prompt", prompt]
        }
        let result = CliBridge.run(args)
        if result.exitCode != 0 {
            let detail = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            friendPolicyError = detail.isEmpty ? "Could not save policy" : detail
            return
        }
        cancelEditingRelayFriend()
        config.refreshRelayFriends()
    }

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
            guard code.v == 2, code.x.count == 64, code.x.allSatisfy({ $0.isHexDigit }) else {
                parsedFriendCode = nil
                friendCodeError = "Invalid encryption key in code"
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
        if config.relayFriends.contains(where: { $0.pubkeyHex == code.k }) {
            friendCodeError = "Already added this friend"
            return
        }

        let name = friendCodeOverrideName.trimmingCharacters(in: .whitespaces)
        var args = ["friend", "add", friendCodeInput]
        if !name.isEmpty {
            args += ["--name", name]
        }
        let r = CliBridge.run(args)
        if r.exitCode != 0 {
            friendCodeError = r.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return
        }
        config.refreshRelayFriends()

        // Reset
        friendCodeInput = ""
        parsedFriendCode = nil
        friendCodeOverrideName = ""
        friendCodeError = nil
    }

    private func approveCloud() {
        let trimmedCloud = cloudURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let savedCloudURL = trimmedCloud.isEmpty ? defaultCloudURL : trimmedCloud
        let relayURL = statusRelayURL.trimmingCharacters(in: .whitespacesAndNewlines)
        approvingCloud = true
        cloudApprovalStatus = "Auditing Cloud..."
        DispatchQueue.global(qos: .userInitiated).async {
            var args = ["backend", "configure", "cloud", "--url", savedCloudURL]
            if !relayURL.isEmpty {
                args += ["--relay-url", relayURL]
            }
            let result = CliBridge.run(args, timeout: 75)
            DispatchQueue.main.async {
                approvingCloud = false
                if result.exitCode == 0 {
                    config.load()
                    backendMode = config.backendMode
                    cloudURL = config.backendMode == "cloud" && !config.backendURL.isEmpty
                        ? config.backendURL
                        : defaultCloudURL
                    statusRelayURL = config.statusRelayURL
                    serverURL = config.serverURL
                    cloudApprovalStatus = "Approved"
                    onSave()
                } else {
                    let detail = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    cloudApprovalStatus = detail.isEmpty ? "Approval failed" : detail
                }
            }
        }
    }

    private func ingestURL(from raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme
        else { return trimmed }

        if scheme == "http" {
            components.scheme = "ws"
        } else if scheme == "https" {
            components.scheme = "wss"
        } else if scheme != "ws" && scheme != "wss" {
            return trimmed
        }

        if components.path.isEmpty || components.path == "/" {
            components.path = "/ingest"
        }
        return components.string ?? trimmed
    }
}
