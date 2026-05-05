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
                Text("Deputies")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("+ New") { showingNewDeputy = true }
                    .buttonStyle(.bordered)
            }

            Text("Authorize remote agents to query your fisherman context. Each deputy is scope-bounded and revocable.")
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


// MARK: - Storage tab

struct StorageTab: View {
    @State private var statusOutput: String = ""
    @State private var selectedKind: String = "none"
    @State private var s3Bucket = ""
    @State private var s3Endpoint = ""
    @State private var s3KeyId = ""
    @State private var s3Secret = ""
    @State private var s3Prefix = ""
    @State private var localPath = ""
    @State private var davUrl = ""
    @State private var davUser = ""
    @State private var davPassword = ""
    @State private var driveClientId = ""
    @State private var driveSecret = ""
    @State private var driveRefresh = ""
    @State private var statusMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Storage Mirror")
                .font(.system(size: 14, weight: .semibold))
            Text("Encrypted backup of your local capture. Required for offline-mirror endpoints.")
                .font(.system(size: 11)).foregroundStyle(.secondary)

            Picker("Backend", selection: $selectedKind) {
                Text("None").tag("none")
                Text("Local filesystem").tag("localfs")
                Text("S3 / R2 / B2").tag("s3")
                Text("WebDAV (Hetzner SB)").tag("webdav")
                Text("Google Drive").tag("drive")
            }
            .pickerStyle(.menu)

            switch selectedKind {
            case "localfs":
                TextField("Path", text: $localPath).textFieldStyle(.roundedBorder)
            case "s3":
                TextField("Bucket", text: $s3Bucket).textFieldStyle(.roundedBorder)
                TextField("Endpoint URL (or empty for AWS)", text: $s3Endpoint).textFieldStyle(.roundedBorder)
                TextField("Access Key ID", text: $s3KeyId).textFieldStyle(.roundedBorder)
                SecureField("Secret Access Key", text: $s3Secret).textFieldStyle(.roundedBorder)
                TextField("Prefix (optional)", text: $s3Prefix).textFieldStyle(.roundedBorder)
            case "webdav":
                TextField("Base URL", text: $davUrl).textFieldStyle(.roundedBorder)
                TextField("Username", text: $davUser).textFieldStyle(.roundedBorder)
                SecureField("Password", text: $davPassword).textFieldStyle(.roundedBorder)
            case "drive":
                Text("See docs/drive-setup.md to mint these.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                TextField("Client ID", text: $driveClientId).textFieldStyle(.roundedBorder)
                SecureField("Client Secret", text: $driveSecret).textFieldStyle(.roundedBorder)
                SecureField("Refresh Token", text: $driveRefresh).textFieldStyle(.roundedBorder)
            default:
                EmptyView()
            }

            HStack {
                Button("Apply") { apply() }
                    .buttonStyle(.borderedProminent)
                if selectedKind == "none" {
                    Button("Disable mirror") { disable() }
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
        .onAppear { reloadStatus() }
    }

    private func reloadStatus() {
        let r = CliBridge.run(["storage", "status", "--text"])
        statusOutput = r.exitCode == 0 ? r.stdout : (r.stderr.isEmpty ? r.stdout : r.stderr)
    }

    private func apply() {
        statusMessage = nil
        let r: CliBridge.Result
        switch selectedKind {
        case "localfs":
            r = CliBridge.run(["storage", "configure-local", "--path", localPath])
        case "s3":
            var args = ["storage", "configure-s3",
                        "--bucket", s3Bucket,
                        "--key-id", s3KeyId,
                        "--secret", s3Secret]
            if !s3Endpoint.isEmpty { args += ["--endpoint", s3Endpoint] }
            if !s3Prefix.isEmpty { args += ["--prefix", s3Prefix] }
            r = CliBridge.run(args)
        case "webdav":
            r = CliBridge.run(["storage", "configure-webdav",
                               "--url", davUrl, "--username", davUser, "--password", davPassword])
        case "drive":
            r = CliBridge.run(["storage", "configure-drive",
                               "--client-id", driveClientId,
                               "--client-secret", driveSecret,
                               "--refresh-token", driveRefresh])
        default:
            disable(); return
        }
        if r.exitCode == 0 {
            statusMessage = "Configured. Restart the daemon for changes to take effect."
            reloadStatus()
        } else {
            statusMessage = "Error: " + r.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    private func disable() {
        let r = CliBridge.run(["storage", "disable"])
        statusMessage = r.exitCode == 0 ? "Disabled." : r.stderr
        reloadStatus()
    }
}


// MARK: - Mirror tab

struct MirrorTab: View {
    @State private var lastToken: String?
    @State private var error: String?
    @State private var statusOutput: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Offline Mirror")
                .font(.system(size: 14, weight: .semibold))
            Text("A mirror endpoint serves agent queries when your laptop is offline. It runs on a server you trust (or on Fisherman Cloud TEE).")
                .font(.system(size: 11)).foregroundStyle(.secondary)

            Divider()

            Text("Self-hosted").font(.system(size: 12, weight: .medium))
            Text("Configure a storage backend first (Storage tab). Then mint a setup token below and paste it on your server after running `fisherman-mirror init <token>`.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            Button("Mint pairing token") { mintToken() }
                .buttonStyle(.borderedProminent)

            if let token = lastToken {
                Text("Setup token (sensitive — exchange privately):")
                    .font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(token).font(.system(size: 10, design: .monospaced))
                        .padding(8)
                        .background(Color.secondary.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                }
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(token, forType: .string)
                }
                .font(.system(size: 11))
            }

            if let error = error {
                Text(error).font(.system(size: 11)).foregroundStyle(.red)
            }

            Divider()

            Text("Fisherman Cloud (TEE)").font(.system(size: 12, weight: .medium))
            Text("Hosted by us, attested via Phala TDX + on-chain compose_hash governance. Coming soon — see docs/tee-deployment.md.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            Button("Coming soon") {}.disabled(true)
        }
    }

    private func mintToken() {
        let r = CliBridge.run(["mirror", "pair-mint"])
        if r.exitCode != 0 {
            error = r.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            lastToken = nil
            return
        }
        for line in r.stdout.split(separator: "\n") {
            let t = String(line).trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("fishmirror:") {
                lastToken = t
                error = nil
                return
            }
        }
        error = "no token in CLI output"
    }
}


// MARK: - Agent tab

struct AgentTab: View {
    @State private var apiKey: String = ""
    @State private var model: String = "openai/gpt-4o-mini"
    @State private var interval: String = "300"
    @State private var enabled: Bool = false
    @State private var statusMessage: String?

    private let plistPath = (NSHomeDirectory() as NSString)
        .appendingPathComponent("Library/LaunchAgents/com.fisherman.agent.plist")

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Status Loop (optional)")
                .font(.system(size: 14, weight: .semibold))
            Text("Periodically read your context, summarize via OpenRouter, publish to friends. Off by default.")
                .font(.system(size: 11)).foregroundStyle(.secondary)

            SecureField("OpenRouter API key (OPENAI_API_KEY)", text: $apiKey)
                .textFieldStyle(.roundedBorder)
            Text("Saved in macOS Keychain — never written to disk in plaintext.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)

            HStack {
                Text("Model:").font(.system(size: 11))
                TextField("openai/gpt-4o-mini", text: $model).textFieldStyle(.roundedBorder)
            }
            HStack {
                Text("Interval (seconds):").font(.system(size: 11))
                TextField("300", text: $interval).frame(width: 100).textFieldStyle(.roundedBorder)
            }

            Toggle("Run automatically (launchd)", isOn: $enabled)
                .onChange(of: enabled) { _, v in v ? install() : uninstall() }

            if let m = statusMessage {
                Text(m).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
        .onAppear {
            if let key = readKeychain() { apiKey = key }
            enabled = FileManager.default.fileExists(atPath: plistPath)
        }
    }

    private func install() {
        guard !apiKey.isEmpty else {
            statusMessage = "Paste an API key first."; enabled = false; return
        }
        guard let cli = CliBridge.fishermanPath() else {
            statusMessage = "fisherman CLI not on PATH."; enabled = false; return
        }
        writeKeychain(apiKey)

        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.fisherman.agent</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(cli)</string>
                <string>agent</string>
                <string>run</string>
                <string>--interval</string>
                <string>\(interval)</string>
                <string>--model</string>
                <string>\(model)</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>OPENAI_API_KEY</key>
                <string>\(apiKey)</string>
            </dict>
            <key>RunAtLoad</key><true/>
            <key>KeepAlive</key><true/>
            <key>StandardOutPath</key>
            <string>/tmp/fisherman-agent.out.log</string>
            <key>StandardErrorPath</key>
            <string>/tmp/fisherman-agent.err.log</string>
        </dict>
        </plist>
        """
        try? plist.write(toFile: plistPath, atomically: true, encoding: .utf8)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["load", plistPath]
        try? p.run()
        p.waitUntilExit()
        statusMessage = "Agent loop running."
    }

    private func uninstall() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["unload", plistPath]
        try? p.run()
        p.waitUntilExit()
        try? FileManager.default.removeItem(atPath: plistPath)
        statusMessage = "Agent loop stopped."
    }

    private func writeKeychain(_ value: String) {
        let q: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: "com.fisherman.agent",
            kSecAttrAccount: "OPENAI_API_KEY",
        ]
        SecItemDelete(q as CFDictionary)
        var add = q
        add[kSecValueData] = value.data(using: .utf8)
        SecItemAdd(add as CFDictionary, nil)
    }

    private func readKeychain() -> String? {
        let q: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: "com.fisherman.agent",
            kSecAttrAccount: "OPENAI_API_KEY",
            kSecReturnData: true,
            kSecMatchLimit: kSecMatchLimitOne,
        ]
        var item: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let s = String(data: data, encoding: .utf8)
        else { return nil }
        return s
    }
}
