import Foundation
import SwiftUI

// MARK: - Deputies tab

struct DeputiesTab: View {
    @State private var deputies: [[String: Any]] = []
    @State private var showingNewDeputy = false
    @State private var newDeputyName = ""
    @State private var newDeputyScopes: Set<String> = ["read:captures"]
    @State private var newDeputyRate = "60"
    @State private var newDeputyExpires = "30d"
    @State private var lastToken: String?
    @State private var error: String?
    @State private var revokingPubkey: String?

    let allScopes = [
        "read:captures",
        "read:transcripts",
        "read:screenshots",
        "read:status",
        "read:friends",
        "publish:status",
        "control:pause",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Agent Access")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("+ New") { showingNewDeputy = true }
                    .buttonStyle(.bordered)
            }

            Text("Authorize agents to query Fisherman through the active backend. Cloud and Self-hosted agents can read while this laptop is offline; Local Only falls back to the laptop relay path.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)

            if deputies.isEmpty {
                Text("(no deputies yet)")
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 8)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(deputies.indices, id: \.self) { i in
                        deputyRow(deputies[i])
                    }
                }
            }

            if let token = lastToken {
                Divider()
                Text("Setup token (paste on agent host):")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(token).font(.system(size: 10, design: .monospaced))
                        .padding(8)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                }
                Button("Copy token") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(token, forType: .string)
                }
            }

            if let error = error {
                Text(error).font(.system(size: 11)).foregroundStyle(.red)
            }
        }
        .onAppear { reload() }
        .sheet(isPresented: $showingNewDeputy) { newDeputySheet }
    }

    private func deputyRow(_ d: [String: Any]) -> some View {
        let name = d["name"] as? String ?? "?"
        let pubkey = d["pubkey"] as? String ?? "?"
        let scopes = (d["scopes"] as? [String]) ?? []
        let rate = d["rate_per_hour"] as? Int ?? 0
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(name).font(.system(size: 12, weight: .semibold))
                Text(String(pubkey.prefix(12)) + "…").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                Spacer()
                Text("\(rate)/hr").font(.system(size: 10)).foregroundStyle(.secondary)
                Button("Revoke") { revoke(pubkey) }
                    .buttonStyle(.borderless)
                    .font(.system(size: 11))
            }
            Text(scopes.joined(separator: ", "))
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
        }
        .padding(8)
        .background(Color.secondary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 4))
    }

    private var newDeputySheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New Deputy").font(.system(size: 14, weight: .semibold))

            TextField("Name (e.g. hermes)", text: $newDeputyName)
                .textFieldStyle(.roundedBorder)

            Text("Scopes").font(.system(size: 12, weight: .medium))
            VStack(alignment: .leading, spacing: 4) {
                ForEach(allScopes, id: \.self) { scope in
                    Toggle(scope, isOn: Binding(
                        get: { newDeputyScopes.contains(scope) },
                        set: { v in if v { newDeputyScopes.insert(scope) } else { newDeputyScopes.remove(scope) } }
                    )).font(.system(size: 11))
                }
            }

            HStack {
                Text("Rate (req/hour):").font(.system(size: 11))
                TextField("60", text: $newDeputyRate).frame(width: 80).textFieldStyle(.roundedBorder)
                Spacer()
                Text("Expires:").font(.system(size: 11))
                TextField("30d", text: $newDeputyExpires).frame(width: 80).textFieldStyle(.roundedBorder)
            }

            Spacer()

            HStack {
                Button("Cancel") { showingNewDeputy = false }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Create") { createDeputy() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20).frame(width: 380, height: 480)
    }

    private func reload() {
        if let arr = CliBridge.runJsonArray(["deputy", "list"]) {
            deputies = arr
        } else {
            deputies = []
        }
    }

    private func createDeputy() {
        guard !newDeputyName.isEmpty, !newDeputyScopes.isEmpty else {
            error = "name and at least one scope required"; return
        }
        var args = ["deputy", "new",
                    "--name", newDeputyName,
                    "--scopes", newDeputyScopes.sorted().joined(separator: ","),
                    "--rate", newDeputyRate]
        if !newDeputyExpires.isEmpty {
            args += ["--expires", newDeputyExpires]
        }
        let r = CliBridge.run(args)
        if r.exitCode != 0 {
            error = r.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return
        }
        // The token is the line that starts with "fishdep:"
        for line in r.stdout.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("fishdep:") {
                lastToken = t
                break
            }
        }
        showingNewDeputy = false
        newDeputyName = ""
        error = nil
        reload()
    }

    private func revoke(_ pubkey: String) {
        let r = CliBridge.run(["deputy", "revoke", pubkey])
        if r.exitCode == 0 { reload() }
        else { error = r.stderr.trimmingCharacters(in: .whitespacesAndNewlines) }
    }
}


// MARK: - Backup tab

struct BackupTab: View {
    @State private var statusOutput: String = ""
    @State private var selectedKind: String = "none"
    @State private var driveClientId = ""
    @State private var driveSecret = ""
    @State private var driveRefresh = ""
    @State private var driveFolderName = "fisherman"
    @State private var statusMessage: String?
    @State private var existingAdvancedKind: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Backup")
                .font(.system(size: 14, weight: .semibold))
            Text("Optional encrypted backup for laptop-local context.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            Text("You do not need this for Self-hosted or Fisherman Cloud. Those backends already store context for their mode.")
                .font(.system(size: 11)).foregroundStyle(.secondary)

            if let existingAdvancedKind {
                Text("A \(existingAdvancedKind) backup is configured from the CLI. The app now only supports Google Drive backup.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Button("Disable backup") { disable() }
                    .buttonStyle(.bordered)
            } else {
                Picker("Backup", selection: $selectedKind) {
                    Text("Off").tag("none")
                    Text("Google Drive").tag("drive")
                }
                .pickerStyle(.segmented)
            }

            if existingAdvancedKind == nil && selectedKind == "drive" {
                Text("Bring your own Google Drive account. Fisherman encrypts snapshots before upload.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text("See docs/drive-setup.md to create the OAuth credentials.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                TextField("Client ID", text: $driveClientId).textFieldStyle(.roundedBorder)
                SecureField("Client Secret", text: $driveSecret).textFieldStyle(.roundedBorder)
                SecureField("Refresh Token", text: $driveRefresh).textFieldStyle(.roundedBorder)
                TextField("Folder Name", text: $driveFolderName).textFieldStyle(.roundedBorder)
            }

            HStack {
                Button(selectedKind == "drive" ? "Save Google Drive Backup" : "Save") { apply() }
                    .buttonStyle(.borderedProminent)
                    .disabled(existingAdvancedKind != nil)
                if selectedKind != "none" || existingAdvancedKind != nil {
                    Button("Disable backup") { disable() }
                }
                Spacer()
                Button("Refresh status") { reloadStatus() }
            }

            if let m = statusMessage {
                Text(m).font(.system(size: 11)).foregroundStyle(.secondary)
            }

            Divider()

            Text("Status")
                .font(.system(size: 12, weight: .medium))
            ScrollView {
                Text(statusOutput.isEmpty ? "(unknown)" : statusOutput)
                    .font(.system(size: 10, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(Color.secondary.opacity(0.05))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
            }
            .frame(height: 90)
        }
        .onAppear {
            loadExistingConfig()
            reloadStatus()
        }
    }

    private func loadExistingConfig() {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".fisherman/storage.json")
        guard let data = try? Data(contentsOf: url),
              let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            selectedKind = "none"
            existingAdvancedKind = nil
            return
        }

        let kind = cfg["kind"] as? String ?? "none"
        if kind == "drive" {
            selectedKind = "drive"
            existingAdvancedKind = nil
            driveClientId = cfg["client_id"] as? String ?? ""
            driveSecret = cfg["client_secret"] as? String ?? ""
            driveRefresh = cfg["refresh_token"] as? String ?? ""
            driveFolderName = cfg["folder_name"] as? String ?? "fisherman"
        } else if kind == "none" {
            selectedKind = "none"
            existingAdvancedKind = nil
        } else {
            selectedKind = "none"
            existingAdvancedKind = kind
        }
    }

    private func reloadStatus() {
        let r = CliBridge.run(["storage", "status", "--text"])
        statusOutput = r.exitCode == 0 ? r.stdout : (r.stderr.isEmpty ? r.stdout : r.stderr)
    }

    private func apply() {
        statusMessage = nil
        let r: CliBridge.Result
        switch selectedKind {
        case "drive":
            r = CliBridge.run(["storage", "configure-drive",
                               "--client-id", driveClientId,
                               "--client-secret", driveSecret,
                               "--refresh-token", driveRefresh,
                               "--folder-name", driveFolderName.isEmpty ? "fisherman" : driveFolderName])
        default:
            disable(); return
        }
        if r.exitCode == 0 {
            statusMessage = "Configured. Restart the daemon for changes to take effect."
            loadExistingConfig()
            reloadStatus()
        } else {
            statusMessage = "Error: " + r.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    private func disable() {
        let r = CliBridge.run(["storage", "disable"])
        statusMessage = r.exitCode == 0 ? "Disabled." : r.stderr
        loadExistingConfig()
        reloadStatus()
    }
}

// MARK: - Activity status tab

struct ActivityStatusTab: View {
    var config: ConfigManager

    @State private var mode: String = "managed"
    @State private var baseURL: String = "https://openrouter.ai/api/v1"
    @State private var model: String = "openai/gpt-4o-mini"
    @State private var apiKey: String = ""
    @State private var apiKeyConfigured: Bool = false
    @State private var managedKeyConfigured: Bool = false
    @State private var externalLLMEnabled: Bool = true
    @State private var statusMessage: String?
    @State private var loading: Bool = false
    @State private var applying: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Activity Status")
                .font(.system(size: 14, weight: .semibold))

            Text("Controls how Fisherman turns private screen context into the short status shown to you and published to friends. These settings apply to the active context home.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)

            Picker("Status generation", selection: $mode) {
                Text("Fisherman-managed").tag("managed")
                Text("My OpenRouter key").tag("byo")
                Text("No LLM").tag("none")
            }
            .pickerStyle(.segmented)

            if mode == "managed" {
                Text(managedCopy)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                HStack {
                    Text("Managed key")
                        .font(.system(size: 11, weight: .medium))
                    Spacer()
                    Text(managedKeyConfigured ? "configured" : "missing")
                        .font(.system(size: 11))
                        .foregroundStyle(managedKeyConfigured ? .green : .orange)
                }
                modelField
            } else if mode == "byo" {
                Text("Use your own OpenRouter-compatible key. Cloud and Self-hosted store it encrypted with that backend's tenant data key; leave it blank to keep the existing key.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                SecureField(apiKeyConfigured ? "Existing key configured" : "OpenRouter API key", text: $apiKey)
                    .textFieldStyle(.roundedBorder)
                modelField
            } else {
                Text("Fisherman will not call an LLM. Status falls back to private keyword categories like coding, terminal, meeting, or browsing.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }

            HStack {
                Button(applying ? "Applying..." : "Apply") { apply() }
                    .buttonStyle(.borderedProminent)
                    .disabled(applying || loading)
                Button("Refresh") { load() }
                    .disabled(loading)
                Spacer()
            }

            if !externalLLMEnabled && mode != "none" {
                Text("The backend operator LLM switch is off, so status will not call a model until that backend enables it.")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }

            if let statusMessage {
                Text(statusMessage)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear { load() }
    }

    private var modelField: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("https://openrouter.ai/api/v1", text: $baseURL)
                .textFieldStyle(.roundedBorder)
            TextField("openai/gpt-4o-mini", text: $model)
                .textFieldStyle(.roundedBorder)
        }
    }

    private var managedCopy: String {
        switch config.backendMode {
        case "cloud":
            return "Fisherman Cloud uses the managed model inside the Cloud CVM after Cloud trust has been approved."
        case "self_hosted":
            return "Your self-hosted server uses the model key configured on that server."
        default:
            return "Local Only keeps raw context on this Mac. Managed status is available when a backend is active."
        }
    }

    private func load() {
        loading = true
        statusMessage = nil
        DispatchQueue.global(qos: .userInitiated).async {
            let object = CliBridge.runJsonObject(["activity-status", "status", "--json"])
            DispatchQueue.main.async {
                if let object {
                    mode = object["mode"] as? String ?? config.statusLLMMode
                    baseURL = object["base_url"] as? String ?? config.statusLLMBaseURL
                    model = object["model"] as? String ?? config.statusLLMModel
                    apiKeyConfigured = object["api_key_configured"] as? Bool ?? false
                    managedKeyConfigured = object["managed_key_configured"] as? Bool ?? false
                    externalLLMEnabled = object["external_llm_enabled"] as? Bool ?? true
                    if let error = object["backend_error"] as? String {
                        statusMessage = error
                    }
                } else {
                    mode = config.statusLLMMode
                    baseURL = config.statusLLMBaseURL
                    model = config.statusLLMModel
                    statusMessage = "Could not read backend status settings."
                }
                loading = false
            }
        }
    }

    private func apply() {
        applying = true
        statusMessage = nil
        config.statusLLMMode = mode
        config.statusLLMBaseURL = baseURL
        config.statusLLMModel = model
        config.save()

        var args = [
            "activity-status", "configure",
            "--mode", mode,
            "--base-url", baseURL,
            "--model", model,
            "--json",
        ]
        if mode == "byo" && !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args += ["--api-key", apiKey.trimmingCharacters(in: .whitespacesAndNewlines)]
        }
        let cliArgs = args

        DispatchQueue.global(qos: .userInitiated).async {
            let result = CliBridge.run(cliArgs)
            let object = result.exitCode == 0
                ? (try? JSONSerialization.jsonObject(with: Data(result.stdout.utf8)) as? [String: Any])
                : nil
            DispatchQueue.main.async {
                if let object {
                    mode = object["mode"] as? String ?? mode
                    baseURL = object["base_url"] as? String ?? baseURL
                    model = object["model"] as? String ?? model
                    apiKeyConfigured = object["api_key_configured"] as? Bool ?? apiKeyConfigured
                    managedKeyConfigured = object["managed_key_configured"] as? Bool ?? managedKeyConfigured
                    externalLLMEnabled = object["external_llm_enabled"] as? Bool ?? externalLLMEnabled
                    apiKey = ""
                    statusMessage = "Saved. Restart the daemon if you changed context-home settings."
                } else {
                    let error = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    statusMessage = error.isEmpty ? "Could not save activity status settings." : error
                }
                applying = false
            }
        }
    }
}
