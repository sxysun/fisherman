import AppKit
import SwiftUI

private struct InstalledSummary {
    var commit: String = "unknown"
    var branch: String = "unknown"
    var subject: String = ""
    var installedAt: String = ""
    var hasApp = false
    var hasVenv = false
}

private struct LatestSummary {
    var checked = false
    var updateAvailable = false
    var commit: String = ""
    var branch: String = ""
    var subject: String = ""
    var error: String = ""
}

private struct BackendVersionSummary {
    var checked = false
    var mode: String = "local"
    var url: String = ""
    var available = false
    var component: String = ""
    var commit: String = ""
    var imageDigest: String = ""
    var error: String = ""
    var detail: String = ""
}

struct UpdatesTab: View {
    var config: ConfigManager

    @State private var installed = InstalledSummary()
    @State private var latest = LatestSummary()
    @State private var backend = BackendVersionSummary()
    @State private var checking = false
    @State private var updating = false
    @State private var message: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Updates")
                .font(.system(size: 13, weight: .semibold))

            appSection
            Divider()
            backendSection

            if let message {
                Text(message)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .onAppear {
            refreshInstalled()
        }
    }

    private var appSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Fisherman app")
                        .font(.system(size: 12, weight: .semibold))
                    Text("Installed \(shortCommit(installed.commit)) on \(installed.branch)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if !installed.subject.isEmpty {
                        Text(installed.subject)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                Spacer()
                statusPill(appUpdateLabel, color: appUpdateColor)
            }

            Text("Updates are installed in place with a backup and rollback check. The menu app may close and reopen during the update.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if latest.checked {
                VStack(alignment: .leading, spacing: 3) {
                    if latest.error.isEmpty {
                        Text("Latest \(shortCommit(latest.commit)) on \(latest.branch)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                        if !latest.subject.isEmpty {
                            Text(latest.subject)
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    } else {
                        Text("Could not check origin: \(latest.error)")
                            .font(.system(size: 10))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            HStack(spacing: 8) {
                Button(checking ? "Checking..." : "Check for Updates") {
                    checkForUpdates()
                }
                .disabled(checking || updating)

                Button(updating ? "Updating..." : "Update Fisherman") {
                    installUpdate()
                }
                .buttonStyle(.borderedProminent)
                .disabled(checking || updating)
            }
        }
    }

    private var backendSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Context home backend")
                        .font(.system(size: 12, weight: .semibold))
                    Text(backendModeLabel)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer()
                statusPill(backendStatusLabel, color: backendStatusColor)
            }

            Text(backendExplanation)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if backend.checked && backend.mode != "local" {
                if backend.available {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("\(backend.component.isEmpty ? "backend" : backend.component) \(shortCommit(backend.commit))")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(selfHostedBackendUpdateAvailable ? .orange : .secondary)
                        if selfHostedBackendUpdateAvailable {
                            Text("Latest code \(shortCommit(latest.commit)); redeploy this backend to update it.")
                                .font(.system(size: 10))
                                .foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if !backend.imageDigest.isEmpty {
                            Text(shortImageDigest(backend.imageDigest))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                } else {
                    Text("Version unavailable: \(backend.detail.isEmpty ? backend.error : backend.detail)")
                        .font(.system(size: 10))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if config.backendMode == "self_hosted" {
                Button("Copy Redeploy Prompt") {
                    copySelfHostedRedeployPrompt()
                }
            }
        }
    }

    private var appUpdateLabel: String {
        if updating { return "updating" }
        if checking { return "checking" }
        if latest.checked && !latest.error.isEmpty { return "check failed" }
        if latest.checked { return latest.updateAvailable ? "update available" : "up to date" }
        return "not checked"
    }

    private var appUpdateColor: Color {
        if updating || checking { return .orange }
        if latest.checked && !latest.error.isEmpty { return .orange }
        if latest.checked && latest.updateAvailable { return .blue }
        if latest.checked { return .green }
        return .secondary
    }

    private var backendModeLabel: String {
        switch config.backendMode {
        case "cloud":
            return "Fisherman Cloud"
        case "self_hosted":
            let url = config.backendURL.isEmpty ? config.serverURL : config.backendURL
            return url.isEmpty ? "Self-hosted" : url
        default:
            return "Local Only"
        }
    }

    private var backendStatusLabel: String {
        if config.backendMode == "local" { return "local" }
        if config.backendMode == "cloud" { return "managed" }
        if selfHostedBackendUpdateAvailable { return "update available" }
        if backend.checked {
            if !backend.available { return "needs redeploy" }
            return latest.checked && latest.error.isEmpty ? "up to date" : "version known"
        }
        return "not checked"
    }

    private var backendStatusColor: Color {
        if config.backendMode == "local" { return .secondary }
        if config.backendMode == "cloud" { return .green }
        if selfHostedBackendUpdateAvailable { return .orange }
        if backend.checked && !backend.available { return .orange }
        if backend.checked { return .green }
        return .secondary
    }

    private var selfHostedBackendUpdateAvailable: Bool {
        guard config.backendMode == "self_hosted",
              backend.checked,
              backend.available,
              latest.checked,
              latest.error.isEmpty
        else { return false }
        return commitsDiffer(backend.commit, latest.commit)
    }

    private var backendExplanation: String {
        switch config.backendMode {
        case "cloud":
            return "Fisherman Cloud is updated by Fisherman CI. Your app still requires Cloud release approval before raw context uploads to a new attested release."
        case "self_hosted":
            return "The Mac app will not SSH into your server or mutate its data. If the backend is old or version metadata is unavailable, ask your context-home operator or agent to redeploy while preserving volumes and Postgres data."
        default:
            return "Local Only has no remote backend to update. Updating the app updates local capture, settings, and local agent access behavior."
        }
    }

    private func refreshInstalled() {
        DispatchQueue.global(qos: .userInitiated).async {
            let raw = CliBridge.runJsonObject(["version", "--json"])
            DispatchQueue.main.async {
                if let raw {
                    installed = parseInstalled(raw)
                }
            }
        }
    }

    private func checkForUpdates() {
        checking = true
        message = "Checking Git origin and active context home version..."
        DispatchQueue.global(qos: .userInitiated).async {
            let raw = CliBridge.runJsonObject(["update-status", "--json"], timeout: 60)
            DispatchQueue.main.async {
                checking = false
                guard let raw else {
                    message = "Could not read update status from the Fisherman CLI."
                    return
                }
                installed = parseInstalled(raw)
                latest = parseLatest(raw)
                backend = parseBackend(raw)
                if latest.updateAvailable {
                    message = "A Fisherman app update is available."
                } else if selfHostedBackendUpdateAvailable {
                    message = "Your self-hosted backend is behind latest code. Copy the redeploy prompt to update it without touching data."
                } else {
                    message = "Fisherman app and active backend check complete."
                }
            }
        }
    }

    private func installUpdate() {
        updating = true
        message = "Updating Fisherman. The menu app may close and reopen."
        DispatchQueue.global(qos: .userInitiated).async {
            let result = CliBridge.run(["upgrade", "--yes", "--force-menubar"], timeout: 1200)
            DispatchQueue.main.async {
                updating = false
                if result.exitCode == 0 {
                    message = "Update complete. Fisherman restarted and the daemon health check passed."
                    refreshInstalled()
                    checkForUpdates()
                } else {
                    let detail = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    let fallback = result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
                    message = "Update failed: \(detail.isEmpty ? fallback : detail)"
                }
            }
        }
    }

    private func copySelfHostedRedeployPrompt() {
        let url = config.backendURL.isEmpty ? config.serverURL : config.backendURL
        let prompt = """
        Update my Fisherman self-hosted context home at \(url).

        Requirements:
        - Preserve all existing data, Postgres databases, Docker volumes, and frame storage.
        - Pull the latest sxysun/fisherman main branch or latest ghcr.io/sxysun/fisherman-mirror image for code commit \(shortCommit(latest.commit)).
        - Redeploy the ingest/API services without changing my Fisherman identity keys.
        - Verify /health and /api/version after restart.
        - Report the running commit, service status, data-preservation checks, and rollback path.
        """
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(prompt, forType: .string)
        message = "Redeploy prompt copied."
    }

    private func statusPill(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 9, weight: .medium))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 5))
    }

    private func parseInstalled(_ raw: [String: Any]) -> InstalledSummary {
        let installed = dict(raw["installed"])
        return InstalledSummary(
            commit: string(installed["commit"]) ?? "unknown",
            branch: string(installed["branch"]) ?? "unknown",
            subject: string(installed["subject"]) ?? "",
            installedAt: string(installed["installed_at"]) ?? "",
            hasApp: bool(installed["has_app"]),
            hasVenv: bool(installed["has_venv"])
        )
    }

    private func parseLatest(_ raw: [String: Any]) -> LatestSummary {
        let latestRaw = dict(raw["latest"])
        return LatestSummary(
            checked: true,
            updateAvailable: bool(raw["update_available"]),
            commit: string(latestRaw["commit"]) ?? "",
            branch: string(latestRaw["branch"]) ?? "",
            subject: string(latestRaw["subject"]) ?? "",
            error: string(raw["update_error"]) ?? ""
        )
    }

    private func parseBackend(_ raw: [String: Any]) -> BackendVersionSummary {
        let backendRaw = dict(raw["backend"])
        let version = dict(backendRaw["version"])
        return BackendVersionSummary(
            checked: true,
            mode: string(backendRaw["mode"]) ?? config.backendMode,
            url: string(backendRaw["backend_url"]) ?? "",
            available: bool(backendRaw["available"]),
            component: string(version["component"]) ?? "",
            commit: string(version["git_commit"]) ?? "",
            imageDigest: string(version["image_digest"]) ?? "",
            error: string(backendRaw["error"]) ?? "",
            detail: string(backendRaw["detail"]) ?? ""
        )
    }

    private func dict(_ value: Any?) -> [String: Any] {
        value as? [String: Any] ?? [:]
    }

    private func string(_ value: Any?) -> String? {
        if let value = value as? String, !value.isEmpty { return value }
        return nil
    }

    private func bool(_ value: Any?) -> Bool {
        if let value = value as? Bool { return value }
        if let value = value as? String {
            return ["1", "true", "yes", "on"].contains(value.lowercased())
        }
        return false
    }

    private func shortCommit(_ value: String) -> String {
        guard !value.isEmpty, value != "unknown" else { return "unknown" }
        return String(value.prefix(12))
    }

    private func shortImageDigest(_ value: String) -> String {
        guard !value.isEmpty else { return "" }
        if value.hasPrefix("sha256:") {
            return "sha256:" + String(value.dropFirst("sha256:".count).prefix(12))
        }
        return String(value.prefix(19))
    }

    private func commitsDiffer(_ left: String, _ right: String) -> Bool {
        let lhs = normalizedCommit(left)
        let rhs = normalizedCommit(right)
        guard !lhs.isEmpty, !rhs.isEmpty else { return false }
        return !lhs.hasPrefix(rhs) && !rhs.hasPrefix(lhs)
    }

    private func normalizedCommit(_ value: String) -> String {
        value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .split(separator: "-", maxSplits: 1)
            .first
            .map(String.init) ?? ""
    }
}
