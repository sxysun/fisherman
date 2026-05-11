import SwiftUI

private struct CloudReleaseReview {
    let url: String
    let composeHash: String
    let gitCommit: String
    let imageDigest: String
    let appID: String
    let liveTLSFingerprint: String
    let attestedTLSFingerprint: String
    let cloudRequiredOK: Bool
    let failures: [String]
    let approvalTitle: String
    let approvalDetail: String
}

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
    @State private var dangerouslySkipCloudAttestation = false
    @State private var selectedTab: SettingsTab = .server
    @State private var cloudApprovalStatus: String?
    @State private var cloudTrustSummary: String?
    @State private var cloudReview: CloudReleaseReview?
    @State private var reviewingCloud = false
    @State private var approvingCloud = false
    @State private var dataOperationInProgress = false
    @State private var dataOperationSummary: String?

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
        case server = "Context Home"
        case identity = "Identity"
        case friends = "Friends"
        case deputies = "Agent Access"
        case agent = "Activity Status"
        case data = "Data"
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
                    case .agent:
                        ActivityStatusTab(config: config)
                    case .data:
                        ContextDataTab(
                            config: config,
                            operationInProgress: $dataOperationInProgress,
                            operationSummary: $dataOperationSummary
                        )
                    case .diagnostics:
                        DiagnosticsTab()
                    }
                }
                .padding(16)
            }

            Divider()

            // Bottom buttons
            HStack {
                if dataOperationInProgress {
                    ProgressView()
                        .controlSize(.small)
                    Text(dataOperationSummary ?? "Data operation in progress...")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .truncationMode(.middle)
                } else {
                    Text("Saved to ~/.fisherman/.env")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                Spacer()
                Button("Cancel") { onCancel() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(dataOperationInProgress)
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
                        if config.serverURL == expectedIngestURL {
                            config.cloudIngestStatus = "enabled"
                            config.cloudIngestBlockReason = ""
                            config.cloudIngestBlockDetail = ""
                        } else if normalizeCloudURL(savedCloudURL) != normalizeCloudURL(previousBackendURL) {
                            config.cloudIngestStatus = "blocked"
                            config.cloudIngestBlockReason = "cloud_approval_required"
                            config.cloudIngestBlockDetail = "Review and approve this Cloud release before raw uploads start."
                        }
                    } else if backendMode == "self_hosted" {
                        config.backendURL = trimmedSelfHostedURL
                        config.serverURL = ingestURL(from: trimmedSelfHostedURL)
                        config.cloudIngestStatus = ""
                        config.cloudIngestBlockReason = ""
                        config.cloudIngestBlockDetail = ""
                    } else {
                        config.backendURL = ""
                        config.serverURL = defaultServerURL
                        config.cloudIngestStatus = ""
                        config.cloudIngestBlockReason = ""
                        config.cloudIngestBlockDetail = ""
                    }
                    config.statusRelayURL = statusRelayURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    config.cloudTrustPolicy = (backendMode == "cloud" && dangerouslySkipCloudAttestation) ? "dangerously_skip" : "strict"
                    config.controlPort = controlPort.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "7892" : controlPort
                    config.displayName = displayName
                    config.save()
                    onSave()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(dataOperationInProgress)
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
            dangerouslySkipCloudAttestation = config.cloudTrustPolicy == "dangerously_skip"
            displayName = config.displayName
            config.refreshRelayFriends()
            loadCloudTrustSummary()
        }
    }

    // MARK: - Server tab

    private var serverTab: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionHeader("Context Home")

            Picker("Mode", selection: $backendMode) {
                Text("Local Only").tag("local")
                Text("Fisherman Cloud").tag("cloud")
                Text("Self-hosted").tag("self_hosted")
            }
            .pickerStyle(.segmented)

            if backendMode == "cloud" {
                hintText("New context is stored and processed by Fisherman Cloud. Changing homes affects new uploads only; history is not copied automatically.")
                hintText("Raw context is processed inside the approved Cloud CVM. Cloud stores ciphertext under your client-held tenant key; after a deploy or restart, the runtime cannot decrypt history again until an approved device reconnects.")
                hintText("Friend status uses separate end-to-end encryption to each friend; the relay does not receive plaintext status.")
                cloudApprovalPanel
            } else if backendMode == "self_hosted" {
                fieldRow("Self-hosted URL", placeholder: "wss://your-server:9999/ingest", text: $selfHostedURL)
                hintText("New context is written to the server you operate. One URL is enough; Fisherman derives activity, history, and agent endpoints automatically.")
            } else {
                hintText("New context stays on this laptop. Agent access requires this Mac to be online, while friend status can still use the encrypted relay.")
            }

            DisclosureGroup("Advanced endpoints", isExpanded: $showAdvancedBackend) {
                VStack(alignment: .leading, spacing: 10) {
                    if backendMode == "cloud" {
                        fieldRow("Fisherman Cloud URL", placeholder: defaultCloudURL, text: $cloudURL)
                        Toggle("Dangerously skip Cloud attestation", isOn: $dangerouslySkipCloudAttestation)
                            .font(.system(size: 11, weight: .medium))
                        if dangerouslySkipCloudAttestation {
                            Text("Unsafe. Raw uploads may continue even when the live Cloud release is unapproved or cannot be verified. Historical Cloud decrypt privacy is not protected in this mode.")
                                .font(.system(size: 10))
                                .foregroundStyle(.red)
                                .fixedSize(horizontal: false, vertical: true)
                        }
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

            hintText("Agent access and activity status use whichever context home is active. Switching homes affects new uploads only; it does not sync old history between homes.")
        }
    }

    private var cloudApprovalPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Cloud release approval")
                        .font(.system(size: 12, weight: .semibold))
                    Text(cloudApprovalBadge)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(cloudApprovalColor)
                }
                Spacer()
                Button(reviewingCloud ? "Reviewing..." : "Review Release") {
                    reviewCloud()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(reviewingCloud || approvingCloud)
            }

            hintText("Review fetches the live TEE quote and compares compose hash, release git, image digest, app identity, and TLS binding against this Mac's approved record.")

            cloudRuntimePanel

            if let cloudReview {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(cloudReview.approvalTitle)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(cloudReview.cloudRequiredOK ? Color.primary : Color.red)
                        Spacer()
                        Text(cloudReview.cloudRequiredOK ? "Audit passed" : "Audit failed")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(cloudReview.cloudRequiredOK ? .green : .red)
                    }

                    Text(cloudReview.approvalDetail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    VStack(alignment: .leading, spacing: 3) {
                        cloudReviewRow("URL", cloudReview.url)
                        cloudReviewRow("Compose", shortHash(cloudReview.composeHash, prefix: "0x"))
                        cloudReviewRow("Git", shortHash(cloudReview.gitCommit))
                        cloudReviewRow("Image", shortImageDigest(cloudReview.imageDigest))
                        cloudReviewRow("App", shortHash(cloudReview.appID))
                        cloudReviewRow("TLS", shortHash(cloudReview.liveTLSFingerprint))
                    }

                    if !cloudReview.failures.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(Array(cloudReview.failures.prefix(4)), id: \.self) { failure in
                                Text("• \(failure)")
                                    .font(.system(size: 10))
                                    .foregroundStyle(.red)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }

                    HStack {
                        Spacer()
                        Button(approvingCloud ? "Approving..." : "Approve & Use This Release") {
                            approveCloud()
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .disabled(!cloudReview.cloudRequiredOK || approvingCloud || reviewingCloud)
                    }
                }
                .padding(10)
                .background(Color.secondary.opacity(0.05))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else if let cloudTrustSummary {
                Text(cloudTrustSummary)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(4)
                if !cloudIngestEnabled {
                    HStack {
                        Spacer()
                        Button(approvingCloud ? "Checking..." : "Finish Setup") {
                            approveCloud()
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .disabled(approvingCloud || reviewingCloud)
                    }
                }
            }

            if let cloudApprovalStatus {
                Text(cloudApprovalStatus)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func cloudReviewRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label)
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.tertiary)
                .frame(width: 48, alignment: .leading)
            Text(value.isEmpty ? "unknown" : value)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private var cloudRuntimePanel: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: cloudIngestEnabled ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(cloudIngestEnabled ? Color.green : Color.orange)
                .font(.system(size: 11))
            VStack(alignment: .leading, spacing: 2) {
                Text(cloudRuntimeTitle)
                    .font(.system(size: 11, weight: .semibold))
                Text(cloudRuntimeDetail)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(8)
        .background(Color.secondary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var cloudIngestEnabled: Bool {
        backendMode == "cloud" && serverURL == ingestURL(from: currentCloudURL())
    }

    private var cloudRuntimeTitle: String {
        if cloudIngestEnabled {
            return "Cloud upload enabled"
        }
        switch config.cloudIngestBlockReason {
        case "cloud_account_not_enabled":
            return "Cloud account not enabled"
        case "cloud_ingest_not_ready":
            return "Cloud ingest not ready"
        case "cloud_approval_required":
            return "Cloud release needs review"
        default:
            return cloudTrustSummary == nil ? "Cloud release needs review" : "Cloud setup incomplete"
        }
    }

    private var cloudRuntimeDetail: String {
        if cloudIngestEnabled {
            return "New raw context uploads to the approved Cloud release. Queued frames drain automatically while the daemon is running."
        }
        if !config.cloudIngestBlockDetail.isEmpty {
            return config.cloudIngestBlockDetail
        }
        if cloudTrustSummary == nil {
            return "Review the live release first. Until approval, Fisherman captures locally and queues Cloud uploads."
        }
        return "The release is approved, but this Mac has not finished account setup. Finish Setup requests hosted Cloud access and enables uploads once the account is active."
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
                    Text(friendPolicyPreview(friend))
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .lineLimit(2)
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

            Text(editingPolicyPreview(friend))
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)

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

    private func friendPolicyPreview(_ friend: RelayFriend) -> String {
        policyPreview(
            name: friend.name,
            audience: friend.audience,
            prompt: friend.policyPrompt ?? "",
            encryptionPubkey: friend.encryptionPubkeyHex
        )
    }

    private func editingPolicyPreview(_ friend: RelayFriend) -> String {
        policyPreview(
            name: friend.name,
            audience: editingFriendAudience,
            prompt: editingFriendPolicyPrompt,
            encryptionPubkey: friend.encryptionPubkeyHex
        )
    }

    private func policyPreview(
        name: String,
        audience: String,
        prompt: String,
        encryptionPubkey: String
    ) -> String {
        let trimmedPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let promptPart = trimmedPrompt.isEmpty
            ? "default privacy filters"
            : "custom instruction plus privacy filters"
        return "Preview: \(name) gets a separate \(audience) status using \(promptPart), then encrypted to \(encryptionPubkey.prefix(12))..."
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

    private func reviewCloud() {
        let savedCloudURL = currentCloudURL()
        reviewingCloud = true
        cloudApprovalStatus = "Reviewing live Cloud release. No private context is uploaded during review."
        cloudReview = nil

        DispatchQueue.global(qos: .userInitiated).async {
            let result = CliBridge.run(
                ["cloud", "audit", savedCloudURL, "--json", "--timeout", "15"],
                timeout: 75
            )
            DispatchQueue.main.async {
                reviewingCloud = false
                if let review = parseCloudReview(stdout: result.stdout, fallbackURL: savedCloudURL) {
                    cloudReview = review
                    cloudApprovalStatus = result.exitCode == 0
                        ? "Review complete. Approve only if the release identity is the one you expect."
                        : "Review found problems. Raw Cloud uploads will stay blocked in strict mode."
                } else {
                    let detail = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    cloudApprovalStatus = detail.isEmpty ? "Could not parse Cloud audit output" : detail
                }
            }
        }
    }

    private func approveCloud() {
        let savedCloudURL = currentCloudURL()
        let relayURL = statusRelayURL.trimmingCharacters(in: .whitespacesAndNewlines)
        approvingCloud = true
        cloudApprovalStatus = "Approving this release. Fisherman will pin its compose hash, git commit, image digest, app identity, and TLS-bound attestation."
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
                    loadCloudTrustSummary()
                    cloudReview = nil
                    if config.serverURL == ingestURL(from: savedCloudURL) {
                        cloudApprovalStatus = "Approved and enabled. Queued frames will upload while the daemon is running."
                    } else if !config.cloudIngestBlockDetail.isEmpty {
                        cloudApprovalStatus = "Release approved, but upload is still blocked: \(config.cloudIngestBlockDetail)"
                    } else {
                        cloudApprovalStatus = "Release approved, but Cloud upload is not enabled for this account yet."
                    }
                } else {
                    let detail = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    cloudApprovalStatus = detail.isEmpty ? "Approval failed" : detail
                }
            }
        }
    }

    private func currentCloudURL() -> String {
        let trimmedCloud = cloudURL.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedCloud.isEmpty ? defaultCloudURL : trimmedCloud
    }

    private func parseCloudReview(stdout: String, fallbackURL: String) -> CloudReleaseReview? {
        guard let data = stdout.data(using: .utf8),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }

        let release = raw["release"] as? [String: Any] ?? [:]
        let app = raw["app"] as? [String: Any] ?? [:]
        let failures = raw["cloud_required_failures"] as? [String]
            ?? raw["errors"] as? [String]
            ?? []
        let cloudRequiredOK = raw["cloud_required_ok"] as? Bool
            ?? (raw["all_required_ok"] as? Bool ?? false)

        let url = raw["mirror_url"] as? String ?? fallbackURL
        let compose = raw["compose_hash"] as? String ?? ""
        let git = release["git_commit"] as? String ?? ""
        let image = release["image_digest"] as? String ?? ""
        let appID = app["app_id"] as? String ?? ""
        let liveTLS = raw["live_tls_fingerprint_hex"] as? String ?? ""
        let attestedTLS = raw["attested_tls_fingerprint_hex"] as? String ?? ""

        let trust = loadCloudTrustRecord()
        let comparison = compareCloudReview(
            url: url,
            compose: compose,
            git: git,
            image: image,
            appID: appID,
            trust: trust
        )

        return CloudReleaseReview(
            url: url,
            composeHash: compose,
            gitCommit: git,
            imageDigest: image,
            appID: appID,
            liveTLSFingerprint: liveTLS,
            attestedTLSFingerprint: attestedTLS,
            cloudRequiredOK: cloudRequiredOK,
            failures: failures,
            approvalTitle: comparison.title,
            approvalDetail: comparison.detail
        )
    }

    private func compareCloudReview(
        url: String,
        compose: String,
        git: String,
        image: String,
        appID: String,
        trust: [String: Any]?
    ) -> (title: String, detail: String) {
        guard let trust else {
            return (
                "No Cloud release approved on this Mac",
                "Approve this release only if the audit passed and this is the Fisherman Cloud endpoint you intend to use."
            )
        }

        var changed: [String] = []
        if normalizeCloudURL(trust["cloud_url"] as? String ?? "") != normalizeCloudURL(url) {
            changed.append("URL")
        }
        if (trust["compose_hash"] as? String ?? "") != compose {
            changed.append("compose")
        }
        if (trust["git_commit"] as? String ?? "") != git {
            changed.append("git")
        }
        if (trust["image_digest"] as? String ?? "") != image {
            changed.append("image")
        }
        let approvedAppID = trust["app_id"] as? String ?? ""
        if !approvedAppID.isEmpty && !appID.isEmpty && approvedAppID != appID {
            changed.append("app")
        }

        if changed.isEmpty {
            return (
                "Matches the approved Cloud release",
                "Strict mode can upload raw context to this release because it matches the pinned trust record on this Mac."
            )
        }
        return (
            "New Cloud release requires approval",
            "Changed: \(changed.joined(separator: ", ")). Until approved, strict mode keeps capturing locally and queues uploads instead of sending raw context."
        )
    }

    private func loadCloudTrustRecord() -> [String: Any]? {
        let trustURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".fisherman/cloud-trust.json")
        guard let data = try? Data(contentsOf: trustURL) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    private func normalizeCloudURL(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed) else {
            return trimmed
        }
        if components.scheme == "ws" {
            components.scheme = "http"
        } else if components.scheme == "wss" {
            components.scheme = "https"
        }
        if components.path == "/ingest" {
            components.path = ""
        }
        components.query = nil
        components.fragment = nil
        return (components.string ?? trimmed).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    private var cloudApprovalBadge: String {
        if approvingCloud {
            return "Approving"
        }
        if reviewingCloud {
            return "Reviewing"
        }
        if let cloudReview {
            return cloudReview.cloudRequiredOK ? "Ready to approve" : "Audit failed"
        }
        if cloudTrustSummary != nil {
            return cloudIngestEnabled ? "Approved and enabled" : "Approved, setup pending"
        }
        return "Not approved"
    }

    private var cloudApprovalColor: Color {
        if let cloudReview {
            return cloudReview.cloudRequiredOK ? .orange : .red
        }
        if reviewingCloud || approvingCloud {
            return .orange
        }
        if cloudTrustSummary == nil || !cloudIngestEnabled {
            return .orange
        }
        return .green
    }

    private func loadCloudTrustSummary() {
        guard let raw = loadCloudTrustRecord() else {
            cloudTrustSummary = nil
            return
        }

        let url = raw["cloud_url"] as? String ?? defaultCloudURL
        let compose = shortHash(raw["compose_hash"] as? String, prefix: "0x")
        let git = shortHash(raw["git_commit"] as? String)
        let image = shortImageDigest(raw["image_digest"] as? String)
        let approvedAt = raw["approved_at"] as? String ?? "unknown time"
        cloudTrustSummary = """
        \(url)
        approved \(approvedAt)
        compose \(compose)  git \(git)  image \(image)
        """
    }

    private func shortHash(_ value: String?, prefix: String = "") -> String {
        let trimmed = (value ?? "").replacingOccurrences(of: "0x", with: "")
        guard !trimmed.isEmpty else { return "unknown" }
        return prefix + String(trimmed.prefix(12))
    }

    private func shortImageDigest(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "unknown" }
        if value.hasPrefix("sha256:") {
            return "sha256:" + String(value.dropFirst("sha256:".count).prefix(12))
        }
        return String(value.prefix(19))
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
