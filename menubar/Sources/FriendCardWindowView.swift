import Foundation
import SwiftUI

/// Friend card popup. It renders the same derived activity-card view used for
/// "me", but its source is encrypted relay status events from `friend status`.
/// This deliberately stays at the activity-row layer: no screenshots, OCR,
/// window titles, queue stats, or raw capture records are fetched or shown.
struct FriendCardWindowView: View {
    let friend: UserActivity

    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @State private var cache: [Date: [ActivityEntry]] = [:]
    @State private var loadingDates: Set<Date> = []
    @State private var lastError: String?

    private var today: Date { Calendar.current.startOfDay(for: Date()) }
    private var isToday: Bool { selectedDate == today }
    private var canGoForward: Bool { selectedDate < today }
    private var target: String {
        let prefix = "relay:"
        if friend.id.hasPrefix(prefix) {
            return String(friend.id.dropFirst(prefix.count))
        }
        return friend.name
    }

    private var displayedHistory: [ActivityEntry] {
        if isToday {
            // The day-scoped relay fetch can come back empty (transient relay
            // hiccup, or it just finished after we'd already seen the friend
            // active). Fall back to the live in-memory history we already hold
            // for them — filtered to today — so the card shows what they're
            // doing instead of "No relay activity on this day".
            if let cached = cache[selectedDate], !cached.isEmpty { return cached }
            return friend.history.filter { entry in
                entry.timestamp >= selectedDate && entry.timestamp < dayAfterSelected
            }
        }
        if let cached = cache[selectedDate] { return cached }
        return []
    }

    private var dayAfterSelected: Date {
        Calendar.current.date(byAdding: .day, value: 1, to: selectedDate)
            ?? selectedDate.addingTimeInterval(24 * 3600)
    }

    private var dateLabel: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        if isToday {
            f.dateFormat = "EEEE MMM d"
            return "Today · \(f.string(from: selectedDate))"
        }
        let cal = Calendar.current
        if let yest = cal.date(byAdding: .day, value: -1, to: today),
           selectedDate == yest {
            f.dateFormat = "EEEE MMM d"
            return "Yesterday · \(f.string(from: selectedDate))"
        }
        f.dateFormat = "EEEE MMM d, yyyy"
        return f.string(from: selectedDate)
    }

    private var emptyMessage: String {
        if loadingDates.contains(selectedDate) { return "Loading…" }
        if let err = lastError { return err }
        return "No relay activity from \(friend.name) on this day."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            navHeader
            sourceNote
            Divider().opacity(0.4)
            DailyCardView(
                history: displayedHistory,
                displayDate: selectedDate,
                emptyMessage: emptyMessage
            )
        }
        .onAppear {
            fetchIfNeeded(selectedDate)
        }
        .onChange(of: selectedDate) { _, newValue in
            fetchIfNeeded(newValue)
        }
    }

    private var navHeader: some View {
        HStack(spacing: 8) {
            Button {
                shiftDay(by: -1)
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Previous day")

            VStack(alignment: .leading, spacing: 1) {
                Text("\(friend.name)'s card")
                    .font(.system(size: 12, weight: .semibold))
                Text(dateLabel)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            .frame(minWidth: 190, alignment: .leading)

            Button {
                shiftDay(by: 1)
            } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(!canGoForward)
            .help(canGoForward ? "Next day" : "Already on today")

            if loadingDates.contains(selectedDate) {
                ProgressView()
                    .controlSize(.small)
            }

            Spacer()

            if !isToday {
                Button("Today") {
                    selectedDate = today
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            Button {
                cache[selectedDate] = nil
                fetchIfNeeded(selectedDate)
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Refresh this friend card")
        }
    }

    private var sourceNote: some View {
        HStack(spacing: 5) {
            Image(systemName: "lock.fill")
                .font(.system(size: 9))
            Text("Encrypted friend status timeline; derived activity rows only.")
                .font(.system(size: 10))
                .lineLimit(1)
        }
        .foregroundStyle(.secondary)
    }

    private func shiftDay(by days: Int) {
        guard let next = Calendar.current.date(byAdding: .day, value: days, to: selectedDate)
        else { return }
        let nextStart = Calendar.current.startOfDay(for: next)
        if nextStart > today { return }
        selectedDate = nextStart
    }

    private func fetchIfNeeded(_ day: Date) {
        let dayStart = Calendar.current.startOfDay(for: day)
        guard cache[dayStart] == nil, !loadingDates.contains(dayStart) else { return }
        guard let dayEnd = Calendar.current.date(byAdding: .day, value: 1, to: dayStart)
        else { return }

        loadingDates.insert(dayStart)
        lastError = nil
        Task {
            let result = await fetchFriendActivityHistory(
                target: target,
                since: dayStart,
                until: dayEnd
            )
            await MainActor.run {
                loadingDates.remove(dayStart)
                switch result {
                case .success(let entries):
                    cache[dayStart] = entries
                case .failure(let err):
                    if cache[dayStart] == nil {
                        lastError = err.localizedDescription
                    }
                }
            }
        }
    }
}

private func fetchFriendActivityHistory(
    target: String,
    since: Date,
    until: Date
) async -> Result<[ActivityEntry], ActivityHistoryFetchError> {
    await withCheckedContinuation { continuation in
        DispatchQueue.global(qos: .utility).async {
            let result = CliBridge.run(
                [
                    "friend", "status", target,
                    "--since", String(since.timeIntervalSince1970),
                    "--limit", "200",
                ],
                timeout: 15
            )
            guard result.exitCode == 0 else {
                continuation.resume(returning: .failure(.requestFailed(result.stderr)))
                return
            }
            guard let data = result.stdout.data(using: .utf8),
                  let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
            else {
                continuation.resume(returning: .failure(.decodeFailed))
                return
            }
            continuation.resume(returning: .success(
                parseFriendActivityRows(rows, since: since, until: until)
            ))
        }
    }
}

private func parseFriendActivityRows(
    _ rows: [[String: Any]],
    since: Date,
    until: Date
) -> [ActivityEntry] {
    var byKey: [String: ActivityEntry] = [:]

    func add(_ entry: ActivityEntry) {
        guard entry.timestamp >= since, entry.timestamp < until else { return }
        let key = "\(Int(entry.timestamp.timeIntervalSince1970))|\(entry.category)|\(entry.status)"
        byKey[key] = entry
    }

    for row in rows {
        let rowTs = numeric(row["ts"])
        guard let digest = row["digest"] as? [String: Any] else { continue }
        if let history = digest["history"] as? [[String: Any]] {
            for raw in history {
                if let entry = parseFriendActivityEntry(raw, fallbackTs: rowTs) {
                    add(entry)
                }
            }
        }
        if let entry = parseFriendActivityEntry(digest, fallbackTs: rowTs) {
            add(entry)
        }
    }

    return byKey.values.sorted { $0.timestamp < $1.timestamp }
}

private func parseFriendActivityEntry(
    _ raw: [String: Any],
    fallbackTs: Double?
) -> ActivityEntry? {
    guard let category = raw["category"] as? String,
          let status = raw["status"] as? String
    else { return nil }

    let timestamp: Date
    if let ts = numeric(raw["ts"]) ?? numeric(raw["timestamp"]) ?? fallbackTs {
        timestamp = Date(timeIntervalSince1970: ts)
    } else if let ts = raw["timestamp"] as? String {
        let primary = ISO8601DateFormatter()
        primary.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fallback = ISO8601DateFormatter()
        fallback.formatOptions = [.withInternetDateTime]
        guard let parsed = primary.date(from: ts) ?? fallback.date(from: ts) else {
            return nil
        }
        timestamp = parsed
    } else {
        return nil
    }

    return ActivityEntry(
        emoji: displayFriendEmoji(raw["emoji"] as? String, category: category),
        category: category,
        status: status,
        timestamp: timestamp
    )
}

private func numeric(_ value: Any?) -> Double? {
    if let value = value as? Double { return value }
    if let value = value as? Int { return Double(value) }
    if let value = value as? NSNumber { return value.doubleValue }
    if let value = value as? String { return Double(value) }
    return nil
}

private func displayFriendEmoji(_ raw: String?, category: String) -> String {
    let fallback: String
    switch category {
    case "coding": fallback = "💻"
    case "debugging": fallback = "🔎"
    case "code review": fallback = "🧾"
    case "reading docs": fallback = "📚"
    case "design": fallback = "🎨"
    case "writing": fallback = "✍️"
    case "chat": fallback = "💬"
    case "email": fallback = "✉️"
    case "meeting": fallback = "📅"
    case "planning": fallback = "📅"
    case "settings": fallback = "🔒"
    case "filling out form": fallback = "📝"
    case "browsing": fallback = "🌐"
    case "news": fallback = "📰"
    case "reading": fallback = "🧠"
    case "gaming": fallback = "🎲"
    case "terminal": fallback = "⌨️"
    case "idle": fallback = "😴"
    default: fallback = "💻"
    }

    let value = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if value.isEmpty { return fallback }
    switch value.lowercased() {
    case ":crossed_swords:": return "⚔️"
    case ":game_die:": return "🎲"
    case ":video_game:": return "🎮"
    case ":computer:", ":laptop:": return "💻"
    case ":mag:": return "🔎"
    case ":memo:": return "🧾"
    case ":books:": return "📚"
    case ":art:": return "🎨"
    case ":speech_balloon:": return "💬"
    case ":email:": return "✉️"
    case ":calendar:": return "📅"
    case ":lock:": return "🔒"
    case ":pencil:", ":memo2:": return "📝"
    case ":globe_with_meridians:": return "🌐"
    case ":newspaper:": return "📰"
    case ":brain:": return "🧠"
    case ":keyboard:": return "⌨️"
    case ":zzz:": return "😴"
    default: break
    }
    if value.hasPrefix(":") || value.unicodeScalars.allSatisfy({ $0.isASCII }) {
        return fallback
    }
    return value
}
