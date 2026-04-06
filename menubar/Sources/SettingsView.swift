import SwiftUI

struct SettingsView: View {
    var config: ConfigManager
    var onSave: () -> Void
    var onCancel: () -> Void

    @State private var serverURL: String = ""
    @State private var authToken: String = ""
    @State private var controlPort: String = ""
    @State private var setupCode: String = ""
    @State private var setupCodeError: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header
            HStack {
                Spacer()
                Image(systemName: "gearshape")
                    .font(.system(size: 14))
                Text("Settings")
                    .font(.system(size: 15, weight: .semibold))
                Spacer()
            }

            // Quick Setup
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 4) {
                    Image(systemName: "bolt.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(.yellow)
                    Text("Quick Setup")
                        .font(.system(size: 12, weight: .semibold))
                }

                TextField("Paste setup code here...", text: $setupCode)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, design: .monospaced))
                    .onSubmit { applySetupCode() }

                if let error = setupCodeError {
                    Text(error)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                }

                Button("Connect") { applySetupCode() }
                    .disabled(setupCode.isEmpty)
            }

            HStack {
                VStack { Divider() }
                Text("or configure manually")
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                    .layoutPriority(1)
                VStack { Divider() }
            }

            // Server URL
            VStack(alignment: .leading, spacing: 4) {
                Text("Server URL")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                TextField("ws://localhost:9999/ingest", text: $serverURL)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, design: .monospaced))
                    .onChange(of: serverURL) { _, newValue in
                        // Detect fish: prefix pasted into the URL field
                        if newValue.hasPrefix("fish:") {
                            setupCode = newValue
                            serverURL = config.serverURL
                            applySetupCode()
                        }
                    }
            }

            // Auth Token (plain NSTextField per MEMORY.md — NSSecureTextField triggers Passwords dialog)
            VStack(alignment: .leading, spacing: 4) {
                Text("Auth Token")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                TextField("(optional)", text: $authToken)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, design: .monospaced))
            }

            // Control Port
            VStack(alignment: .leading, spacing: 4) {
                Text("Control Port")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                TextField("7892", text: $controlPort)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, design: .monospaced))
            }

            Divider()

            // Buttons
            HStack {
                Spacer()
                Button("Cancel") {
                    onCancel()
                }
                .keyboardShortcut(.cancelAction)

                Button("Save") {
                    config.serverURL = serverURL
                    config.authToken = authToken
                    config.controlPort = controlPort
                    config.save()
                    onSave()
                }
                .keyboardShortcut(.defaultAction)
            }

            // Footer
            Text("Saved to ~/.fisherman/.env")
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
        }
        .padding(20)
        .frame(width: 340)
        .onAppear {
            serverURL = config.serverURL
            authToken = config.authToken
            controlPort = config.controlPort
        }
    }

    private func applySetupCode() {
        setupCodeError = nil
        guard let parsed = ConfigManager.parseSetupCode(setupCode) else {
            setupCodeError = "Invalid setup code"
            return
        }
        config.serverURL = parsed.url
        config.authToken = parsed.token
        config.controlPort = controlPort // keep existing control port
        config.save()
        onSave()
    }
}
