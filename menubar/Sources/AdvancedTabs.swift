import Foundation
import AppKit
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Deputies tab

struct DeputiesTab: View {
    @State private var deputies: [[String: Any]] = []
    @State private var showingNewDeputy = false
    @State private var newDeputyName = ""
    @State private var newDeputyScopes: Set<String> = ["read:captures"]
    @State private var newDeputyRate = "60"
    @State private var newDeputyExpires = "30d"
    @State private var lastToken: String?
    @State private var lastAgentInstructions: String?
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
                Text("Setup token")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(token).font(.system(size: 10, design: .monospaced))
                        .padding(8)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                }
                HStack {
                    Button("Copy token") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(token, forType: .string)
                    }
                    if let instructions = lastAgentInstructions {
                        Button("Copy agent instructions") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(instructions, forType: .string)
                        }
                    }
                }
                if let instructions = lastAgentInstructions {
                    Text("Agent instructions")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                    ScrollView {
                        Text(instructions)
                            .font(.system(size: 10, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                    }
                    .frame(height: 130)
                    .background(Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
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
        let requestedName = newDeputyName
        let requestedScopes = newDeputyScopes.sorted()
        var args = ["deputy", "new",
                    "--name", requestedName,
                    "--scopes", requestedScopes.joined(separator: ","),
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
                lastAgentInstructions = Self.agentInstructions(
                    token: t,
                    name: requestedName,
                    scopes: requestedScopes
                )
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

    private static func agentInstructions(token: String, name: String, scopes: [String]) -> String {
        """
        You have been granted scoped Fisherman Agent Access as `\(name)`.

        Treat the `fishdep:` setup token as a secret. Do not commit it, paste it into logs, or send it to any service other than the Fisherman CLI on the agent host.

        Register this agent host once:

        ```bash
        fisherman deputy register '\(token)'
        ```

        Then query through the registered deputy config:

        ```bash
        fisherman status --text
        fisherman query --since 30m --limit 20 --text
        fisherman transcripts --since 2h --limit 20 --text
        ```

        Use `--source secondary` for Cloud/Self-hosted, `--source primary` for laptop relay, and `--source auto` by default.

        Allowed scopes: \(scopes.joined(separator: ", "))
        """
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

            Text("Controls how Fisherman turns private screen context into the short status shown to you and published to friends.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            VStack(alignment: .leading, spacing: 6) {
                Text("Active context home")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(backendSummary)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Status generation")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Picker("", selection: $mode) {
                    Text("Fisherman-managed").tag("managed")
                    Text("My OpenRouter key").tag("byo")
                    Text("No LLM").tag("none")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
            }

            if mode == "managed" {
                Text(managedCopy)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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
                Text(byoCopy)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                SecureField(apiKeyConfigured ? "Existing key configured" : "OpenRouter API key", text: $apiKey)
                    .textFieldStyle(.roundedBorder)
                modelField
            } else {
                Text("Fisherman will not call an LLM. Status falls back to private keyword categories like coding, terminal, meeting, or browsing.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var modelField: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("LLM endpoint")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("https://openrouter.ai/api/v1", text: $baseURL)
                .textFieldStyle(.roundedBorder)
            Text("Model")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("openai/gpt-4o-mini", text: $model)
                .textFieldStyle(.roundedBorder)
        }
    }

    private var managedCopy: String {
        switch config.backendMode {
        case "cloud":
            return "Fisherman Cloud uses Fisherman's managed key from inside the Cloud CVM. Selected app, window-title, and OCR snippets are sent to the configured LLM provider for status generation."
        case "self_hosted":
            return "Your self-hosted server uses the managed key configured in that server's environment. This app does not SSH into the server or edit server env vars."
        default:
            return "Local Only keeps raw context on this Mac. Managed status requires a Cloud or self-hosted context home."
        }
    }

    private var byoCopy: String {
        switch config.backendMode {
        case "cloud":
            return "Apply sends this key to Fisherman Cloud through signed FishKey auth. Cloud stores it encrypted in your tenant settings and decrypts it only inside the backend runtime to generate status."
        case "self_hosted":
            return "Apply sends this key to your self-hosted backend through signed FishKey auth. The backend stores it encrypted in your tenant row and uses it for its own status worker."
        default:
            return "Local Only saves the mode, endpoint, and model on this Mac. BYO key storage is only supported when Cloud or Self-hosted is the active context home."
        }
    }

    private var backendSummary: String {
        switch config.backendMode {
        case "cloud":
            let url = config.backendURL.isEmpty ? "https://fisherman.teleport.computer" : config.backendURL
            return "Fisherman Cloud  \(url)"
        case "self_hosted":
            let url = config.backendURL.isEmpty ? config.serverURL : config.backendURL
            return "Self-hosted  \(url)"
        default:
            return "Local Only  this Mac"
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
                    statusMessage = savedMessage(object)
                } else {
                    let error = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    statusMessage = error.isEmpty ? "Could not save activity status settings." : error
                }
                applying = false
            }
        }
    }

    private func savedMessage(_ object: [String: Any]) -> String {
        let backendMode = object["backend_mode"] as? String ?? config.backendMode
        let backendURL = object["backend_url"] as? String ?? config.backendURL
        switch backendMode {
        case "cloud":
            return "Saved on Fisherman Cloud at \(backendURL). The Cloud status worker will use these settings."
        case "self_hosted":
            return "Saved on your self-hosted backend at \(backendURL). The server-side status worker will use these settings."
        default:
            return "Saved locally in ~/.fisherman/.env. Local Only does not run a remote status worker."
        }
    }
}

// MARK: - Context data tab

struct ContextDataTab: View {
    var config: ConfigManager
    @Binding var operationInProgress: Bool
    @Binding var operationSummary: String?

    @State private var exportSince: String = ""
    @State private var exportLimit: String = "5000"
    @State private var includeImages: Bool = false
    @State private var deleteSince: String = "30d"
    @State private var deleteConfirm: String = ""
    @State private var statusMessage: String?
    @State private var operationMessage: String?
    @State private var busy: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Context Data")
                .font(.system(size: 14, weight: .semibold))

            Text("Export, import, or delete the active context home. Switching between Local Only, Fisherman Cloud, and Self-hosted affects new uploads only; use this tab to move history intentionally.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if busy {
                HStack(alignment: .top, spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                        .padding(.top, 1)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(operationMessage ?? "Working...")
                            .font(.system(size: 11, weight: .medium))
                        Text("The final file appears when the operation finishes. Leave the Fisherman menu bar app running; Save and Cancel are disabled while this is active.")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(8)
                .background(Color.accentColor.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Active home")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(activeHomeSummary)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }

            Divider()

            Text("Export")
                .font(.system(size: 12, weight: .semibold))
            HStack {
                TextField("Since, e.g. 7d or 24h (blank = newest)", text: $exportSince)
                    .textFieldStyle(.roundedBorder)
                TextField("Limit", text: $exportLimit)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 72)
            }
            Toggle("Include screenshots", isOn: Binding(
                get: { includeImages },
                set: { value in
                    includeImages = value
                    if value && exportLimit.trimmingCharacters(in: .whitespacesAndNewlines) == "5000" {
                        exportLimit = "100"
                    }
                }
            ))
                .font(.system(size: 11))
            Text(includeImages ? "The history file will contain raw screenshots. Screenshot exports default to 100 frames; increase the limit only when you need a large private file." : "Default export includes OCR, app/window metadata, URLs, and transcripts, but not screenshots.")
                .font(.system(size: 10))
                .foregroundStyle(includeImages ? .orange : .secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Export History File") { exportArchive() }
                .buttonStyle(.borderedProminent)
                .disabled(busy)

            Divider()

            Text("Import")
                .font(.system(size: 12, weight: .semibold))
            Text("Import writes a Fisherman history JSON file into the active home. It does not delete the source home.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            Button("Import History File") { importArchive() }
                .buttonStyle(.bordered)
                .disabled(busy)

            Divider()

            Text("Delete")
                .font(.system(size: 12, weight: .semibold))
            TextField("Delete records newer than, e.g. 30d", text: $deleteSince)
                .textFieldStyle(.roundedBorder)
            TextField("Type DELETE to enable deletion", text: $deleteConfirm)
                .textFieldStyle(.roundedBorder)
            HStack {
                Button("Dry Run") { deleteContext(dryRun: true) }
                    .buttonStyle(.bordered)
                    .disabled(busy)
                Button("Delete Matching Context") { deleteContext(dryRun: false) }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    .disabled(busy || deleteConfirm != "DELETE")
            }

            if let statusMessage {
                Text(statusMessage)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var activeHomeSummary: String {
        switch config.backendMode {
        case "cloud":
            return "Fisherman Cloud  \(config.backendURL.isEmpty ? "https://fisherman.teleport.computer" : config.backendURL)"
        case "self_hosted":
            return "Self-hosted  \((config.backendURL.isEmpty ? config.serverURL : config.backendURL))"
        default:
            return "Local Only  ~/.fisherman/frames and ~/.fisherman/audio"
        }
    }

    private func exportArchive() {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = "Fisherman History \(Self.dateSlug()).json"
        panel.allowedContentTypes = [.json]
        panel.allowsOtherFileTypes = false
        panel.isExtensionHidden = false
        panel.message = "Exports a Fisherman history JSON file. This is not a zip archive."
        guard panel.runModal() == .OK, let selectedURL = panel.url else { return }
        let url = Self.jsonFileURL(selectedURL)
        let displayPath = Self.displayPath(url.path)
        let limit = exportLimit.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "5000" : exportLimit
        startOperation(
            includeImages
                ? "Exporting screenshots to \(displayPath)"
                : "Exporting history to \(displayPath)"
        )
        statusMessage = "Export started: \(displayPath)\nThe file appears when export finishes."
        var args = [
            "context", "export",
            "--home", "active",
            "--output", url.path,
            "--limit", limit,
        ]
        let since = exportSince.trimmingCharacters(in: .whitespacesAndNewlines)
        if !since.isEmpty { args += ["--since", since] }
        if includeImages { args += ["--include-images"] }
        runLong(args, successPrefix: "Export complete. Open this .json file with a text editor or import it with Fisherman; it is not a zip archive.")
    }

    private func importArchive() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        panel.allowsOtherFileTypes = false
        panel.message = "Choose a Fisherman history JSON file exported from Settings -> Data."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        startOperation("Importing \(Self.displayPath(url.path))")
        statusMessage = "Importing history file..."
        runLong(["context", "import", url.path, "--home", "active"], successPrefix: "Import complete.")
    }

    private static func jsonFileURL(_ url: URL) -> URL {
        if url.pathExtension.lowercased() == "json" {
            return url
        }
        return url.appendingPathExtension("json")
    }

    private func deleteContext(dryRun: Bool) {
        startOperation(dryRun ? "Counting matching context..." : "Deleting matching context...")
        statusMessage = dryRun ? "Counting matching context..." : "Deleting matching context..."
        var args = ["context", "delete", "--home", "active"]
        let since = deleteSince.trimmingCharacters(in: .whitespacesAndNewlines)
        if !since.isEmpty {
            args += ["--since", since]
        } else {
            args += ["--all"]
        }
        if dryRun {
            args += ["--dry-run"]
        } else {
            args += ["--confirm", "DELETE"]
        }
        runLong(args, successPrefix: dryRun ? "Dry run complete." : "Delete complete.")
    }

    private func startOperation(_ message: String) {
        busy = true
        operationMessage = message
        operationInProgress = true
        operationSummary = message
    }

    private func runLong(_ args: [String], successPrefix: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = CliBridge.run(args, timeout: 1800)
            DispatchQueue.main.async {
                busy = false
                operationMessage = nil
                operationInProgress = false
                operationSummary = nil
                if result.exitCode == 0 {
                    statusMessage = successPrefix + "\n" + result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
                } else {
                    let stderr = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    let message = stderr.isEmpty ? result.stdout.trimmingCharacters(in: .whitespacesAndNewlines) : stderr
                    statusMessage = Self.friendlyCommandError(message)
                }
            }
        }
    }

    private static func displayPath(_ path: String) -> String {
        path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }

    private static func friendlyCommandError(_ raw: String) -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let jsonStart = text.firstIndex(of: "{") else {
            return text.isEmpty ? "Command failed." : text
        }
        let jsonText = String(text[jsonStart...])
        guard
            let data = jsonText.data(using: .utf8),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return text.isEmpty ? "Command failed." : text
        }

        let error = obj["error"] as? String ?? ""
        let code = obj["code"] as? String ?? error
        let detail = (obj["detail"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let detailSuffix = detail.isEmpty ? "" : "\n\nDetails: \(detail)"

        switch code {
        case "cloud_ingest_unavailable":
            return "Fisherman Cloud ingest is temporarily unavailable. Try again in a moment or open Diagnostics to check Cloud health.\(detailSuffix)"
        case "tenant_key_unavailable":
            return "Fisherman Cloud cannot decrypt this context until this device reconnects and provides its client-held key. Make sure Fisherman is connected, then retry.\(detailSuffix)"
        default:
            if !error.isEmpty {
                return "Backend error: \(error)\(detailSuffix)"
            }
            return text.isEmpty ? "Command failed." : text
        }
    }

    private static func dateSlug() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: Date())
    }
}
