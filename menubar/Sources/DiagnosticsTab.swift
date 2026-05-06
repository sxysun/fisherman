import SwiftUI
import Foundation

/// Settings → Diagnostics. Native equivalent of `fisherman doctor` for
/// users who don't open a terminal: runs the same checks (menubar
/// process, daemon control port, screenpipe binary + process + HTTP,
/// /Applications bundle), shows them as green/red rows, and offers a
/// one-click "Repair" button that runs `fisherman repair` (re-registers
/// the app with LaunchServices, kills zombies, relaunches everything).
///
/// We deliberately shell out to the CLI rather than re-implementing
/// the diagnostics in Swift: keeps a single source of truth and means
/// the menubar UI automatically picks up new checks the CLI adds.
struct DiagnosticsTab: View {

    @State private var rows: [(name: String, ok: Bool, detail: String)] = []
    @State private var running: Bool = false
    @State private var lastError: String?
    @State private var lastRunAt: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Diagnostics", systemImage: "stethoscope")
                    .font(.headline)
                Spacer()
                if running {
                    ProgressView().scaleEffect(0.6)
                }
                Button {
                    refresh()
                } label: {
                    Label("Recheck", systemImage: "arrow.clockwise")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .disabled(running)
            }

            Text("These are the same checks as `fisherman doctor`. Red rows usually clear with one click of Repair.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Divider()

            if let err = lastError {
                Label(err, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if rows.isEmpty && !running {
                Text("Click Recheck to run diagnostics.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            ForEach(rows, id: \.name) { row in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: row.ok
                          ? "checkmark.circle.fill"
                          : "exclamationmark.triangle.fill")
                        .foregroundStyle(row.ok ? Color.green : Color.orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.name)
                            .font(.system(size: 12, weight: .medium))
                        Text(row.detail)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                }
                .padding(.vertical, 2)
            }

            if let ts = lastRunAt {
                Text("Last checked \(ts.formatted(date: .omitted, time: .standard))")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }

            if !rows.isEmpty {
                Divider()
                let anyRed = rows.contains(where: { !$0.ok })
                HStack {
                    Button {
                        repair()
                    } label: {
                        Label("Repair", systemImage: "wrench.and.screwdriver")
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(running || !anyRed)

                    if !anyRed {
                        Text("All systems healthy.")
                            .font(.caption)
                            .foregroundStyle(.green)
                    } else {
                        Text("Repair will re-register the app with LaunchServices and relaunch everything.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
            }
        }
        .onAppear {
            if rows.isEmpty { refresh() }
        }
    }

    // MARK: - Actions

    private func refresh() { invoke(args: ["doctor", "--json"]) }
    private func repair()  { invoke(args: ["repair", "--json"], timeout: 60) }

    private func invoke(args: [String], timeout: TimeInterval = 15) {
        running = true
        lastError = nil
        DispatchQueue.global(qos: .userInitiated).async {
            let r = CliBridge.run(args, timeout: timeout)
            // doctor/repair return non-zero exit code when any row is
            // red — but stdout is still valid JSON. Don't gate on exit.
            let parsed = parseRows(r.stdout)
            DispatchQueue.main.async {
                running = false
                lastRunAt = Date()
                if let parsed = parsed {
                    rows = parsed
                } else if !r.stderr.isEmpty {
                    lastError = "fisherman \(args.joined(separator: " ")) failed: \(r.stderr.prefix(200))"
                } else {
                    lastError = "Could not parse diagnostics output."
                }
            }
        }
    }

    private func parseRows(_ stdout: String) -> [(name: String, ok: Bool, detail: String)]? {
        guard let data = stdout.data(using: .utf8),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        // Stable presentation order regardless of dict iteration order.
        let order = ["menubar", "daemon", "screenpipe_binary",
                     "screenpipe_process", "screenpipe_http", "app_bundle"]
        var out: [(String, Bool, String)] = []
        for key in order {
            if let r = dict[key] as? [String: Any] {
                let ok = (r["ok"] as? Bool) ?? false
                let detail = (r["detail"] as? String) ?? ""
                out.append((label(for: key), ok, detail))
            }
        }
        // Append any new keys the CLI starts emitting.
        for (k, v) in dict where !order.contains(k) {
            if let r = v as? [String: Any] {
                let ok = (r["ok"] as? Bool) ?? false
                let detail = (r["detail"] as? String) ?? ""
                out.append((label(for: k), ok, detail))
            }
        }
        return out
    }

    private func label(for key: String) -> String {
        switch key {
        case "menubar":            return "Menu bar app"
        case "daemon":             return "Daemon (control port)"
        case "screenpipe_binary":  return "Screenpipe binary"
        case "screenpipe_process": return "Screenpipe process"
        case "screenpipe_http":    return "Screenpipe HTTP (127.0.0.1:3030)"
        case "app_bundle":         return "/Applications/Fisherman.app"
        default:                   return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}
