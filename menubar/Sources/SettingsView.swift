import SwiftUI

struct SettingsView: View {
    var config: ConfigManager
    var onSave: () -> Void
    var onCancel: () -> Void

    @State private var serverURL: String = ""
    @State private var authToken: String = ""
    @State private var controlPort: String = ""

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

            // Server URL
            VStack(alignment: .leading, spacing: 4) {
                Text("Server URL")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                TextField("ws://localhost:9999/ingest", text: $serverURL)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, design: .monospaced))
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
}
