import SwiftUI
import AppKit
import Foundation

// MARK: - Run grouping

struct ActivityRun: Identifiable {
    let id = UUID()
    let category: String
    let startTs: Date
    let endTs: Date
    let entries: [ActivityEntry]      // chronological, all share category
    let dominantEmoji: String
    let distinctStatuses: [String]    // deduped in order
    let color: NSColor

    var eventCount: Int { entries.count }

    var durationText: String {
        let s = Int(endTs.timeIntervalSince(startTs))
        if s < 60 { return "\(s)s" }
        let m = s / 60
        if m < 60 { return "\(m)m" }
        let h = m / 60
        let rem = m % 60
        return rem > 0 ? "\(h)h\(String(format: "%02d", rem))m" : "\(h)h"
    }
}

private func groupRuns(_ history: [ActivityEntry]) -> [ActivityRun] {
    // Server returns newest-first; we want chronological for the card.
    let chronological = history.sorted(by: { $0.timestamp < $1.timestamp })
    var runs: [ActivityRun] = []
    var current: (category: String, entries: [ActivityEntry])? = nil

    func emit(_ buffer: (category: String, entries: [ActivityEntry])) {
        let entries = buffer.entries
        guard !entries.isEmpty else { return }
        var emojiCount: [String: Int] = [:]
        var distinct: [String] = []
        var seen = Set<String>()
        for e in entries {
            let em = e.emoji.trimmingCharacters(in: CharacterSet.whitespaces)
            if !em.isEmpty { emojiCount[em, default: 0] += 1 }
            let s = e.status.trimmingCharacters(in: CharacterSet.whitespaces)
            if !s.isEmpty && !seen.contains(s) {
                seen.insert(s); distinct.append(s)
            }
        }
        let dominant = emojiCount.max(by: { $0.value < $1.value })?.key ?? "·"
        let color = ActivityCategory.from(buffer.category).color
        runs.append(ActivityRun(
            category: buffer.category,
            startTs: entries.first!.timestamp,
            endTs: entries.last!.timestamp,
            entries: entries,
            dominantEmoji: dominant,
            distinctStatuses: distinct,
            color: color
        ))
    }

    for entry in chronological {
        let cat = entry.category.lowercased().trimmingCharacters(in: CharacterSet.whitespaces)
        if var c = current, c.category == cat {
            c.entries.append(entry)
            current = c
        } else {
            if let c = current { emit(c) }
            current = (category: cat, entries: [entry])
        }
    }
    if let c = current { emit(c) }
    return runs
}

private struct CategoryStat: Identifiable {
    let id = UUID()
    let category: String
    let count: Int
    let share: Double
    let color: NSColor
}

private func categoryStats(_ entries: [ActivityEntry]) -> [CategoryStat] {
    guard !entries.isEmpty else { return [] }
    var counts: [String: Int] = [:]
    for e in entries {
        let key = e.category.lowercased().trimmingCharacters(in: CharacterSet.whitespaces)
        let bucket = key.isEmpty ? "other" : key
        counts[bucket, default: 0] += 1
    }
    let total = max(1, entries.count)
    return counts
        .map { (cat, n) in
            CategoryStat(category: cat, count: n,
                         share: Double(n) / Double(total),
                         color: ActivityCategory.from(cat).color)
        }
        .sorted(by: { $0.count > $1.count })
}

private func fmtHHmm(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f.string(from: d)
}

private func dayParts(today: Date = Date()) -> (weekday: String, date: String, year: String) {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US")
    f.dateFormat = "EEEE"
    let weekday = f.string(from: today)
    f.dateFormat = "MMMM d"
    let date = f.string(from: today)
    f.dateFormat = "yyyy"
    let year = f.string(from: today)
    return (weekday, date, year)
}

// MARK: - DailyCardView

struct DailyCardView: View {
    /// `history` should be the user's chronological activity entries —
    /// typically `me.history` as populated by StatusPoller.pollAllHistory.
    let history: [ActivityEntry]

    private var runs: [ActivityRun] { groupRuns(history) }
    private var stats: [CategoryStat] { categoryStats(history) }
    private var hoursActive: Int {
        let cal = Calendar.current
        let hours = Set(history.map { cal.component(.hour, from: $0.timestamp) })
        return hours.count
    }
    private var topCategory: String {
        stats.first?.category ?? "—"
    }
    private var firstLastTimeText: String {
        let chronological = history.sorted(by: { $0.timestamp < $1.timestamp })
        guard let first = chronological.first, let last = chronological.last else { return "" }
        return "\(fmtHHmm(first.timestamp)) → \(fmtHHmm(last.timestamp))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            heroHeader

            if !history.isEmpty {
                statTiles
                categoryShareBar
                Divider().opacity(0.4)
                activityTimeline
            } else {
                emptyState
            }
        }
    }

    private var heroHeader: some View {
        let parts = dayParts()
        return VStack(alignment: .leading, spacing: 2) {
            Text("DAILY CARD")
                .font(.system(size: 9, weight: .semibold))
                .tracking(1.2)
                .foregroundStyle(.tertiary)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(parts.weekday)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color.green.opacity(0.85))
                Text(parts.date)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.primary)
                Text(parts.year)
                    .font(.system(size: 14, weight: .regular))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                if !firstLastTimeText.isEmpty {
                    Text(firstLastTimeText)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var statTiles: some View {
        HStack(spacing: 6) {
            statTile(label: "events", value: "\(history.count)")
            statTile(label: "categories", value: "\(stats.count)")
            statTile(label: "active hrs", value: "\(hoursActive)")
            statTile(label: "top focus", value: topCategory, isText: true)
        }
    }

    private func statTile(label: String, value: String, isText: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label.uppercased())
                .font(.system(size: 8, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.system(size: isText ? 11 : 16, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.04))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private var categoryShareBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { _ in
                HStack(spacing: 1) {
                    ForEach(stats) { s in
                        Rectangle()
                            .fill(Color(nsColor: s.color))
                            .frame(width: nil)
                            .frame(maxWidth: .infinity)
                            .layoutPriority(s.share)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 3))
            }
            .frame(height: 6)
            FlowLayout(spacing: 5) {
                ForEach(stats.prefix(6)) { s in
                    HStack(spacing: 4) {
                        Circle()
                            .fill(Color(nsColor: s.color))
                            .frame(width: 6, height: 6)
                        Text(s.category)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                        Text("\(s.count)")
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.white.opacity(0.03))
                    .clipShape(Capsule())
                }
            }
        }
    }

    private var activityTimeline: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(runs) { run in
                    runRow(run: run)
                }
            }
        }
        .frame(maxHeight: 280)
    }

    private func runRow(run: ActivityRun) -> some View {
        let isSingle = run.entries.count == 1 && run.distinctStatuses.count <= 1
        return Group {
            if isSingle {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(fmtHHmm(run.startTs))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .frame(width: 36, alignment: .leading)
                    Text(run.dominantEmoji)
                        .font(.system(size: 12))
                        .frame(width: 16, alignment: .center)
                    Text(run.category)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .frame(minWidth: 70, alignment: .leading)
                        .lineLimit(1)
                    Text(run.distinctStatuses.first ?? "")
                        .font(.system(size: 11))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 3)
                .padding(.leading, 6)
                .overlay(
                    Rectangle()
                        .fill(Color(nsColor: run.color))
                        .frame(width: 2),
                    alignment: .leading
                )
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(fmtHHmm(run.startTs))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .frame(width: 36, alignment: .leading)
                        Text(run.dominantEmoji)
                            .font(.system(size: 13))
                        Text(run.category)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.primary)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text("\(run.eventCount) events · \(run.durationText)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                    ForEach(run.distinctStatuses.prefix(4), id: \.self) { s in
                        HStack(alignment: .top, spacing: 4) {
                            Text("·").foregroundStyle(.tertiary)
                            Text(s)
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .padding(.leading, 56)
                    }
                    if run.distinctStatuses.count > 4 {
                        Text("+ \(run.distinctStatuses.count - 4) more")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                            .padding(.leading, 60)
                    }
                }
                .padding(.vertical, 4)
                .padding(.leading, 6)
                .overlay(
                    Rectangle()
                        .fill(Color(nsColor: run.color))
                        .frame(width: 2),
                    alignment: .leading
                )
            }
        }
    }

    private var emptyState: some View {
        Text("No activity for today yet — the categorizer is warming up.")
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
            .padding(.vertical, 12)
    }
}

// MARK: - FlowLayout (wraps chips onto new lines)

/// Simple wrapping HStack that supports SwiftUI's Layout protocol (macOS 13+).
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? .infinity
        var rowW: CGFloat = 0
        var totalH: CGFloat = 0
        var rowH: CGFloat = 0
        for sv in subviews {
            let s = sv.sizeThatFits(.unspecified)
            if rowW + s.width > maxW, rowW > 0 {
                totalH += rowH + spacing
                rowW = s.width + spacing
                rowH = s.height
            } else {
                rowW += s.width + spacing
                rowH = max(rowH, s.height)
            }
        }
        totalH += rowH
        return CGSize(width: maxW.isFinite ? maxW : rowW, height: totalH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let maxW = bounds.width
        var x: CGFloat = bounds.minX
        var y: CGFloat = bounds.minY
        var rowH: CGFloat = 0
        for sv in subviews {
            let s = sv.sizeThatFits(.unspecified)
            if x + s.width > bounds.minX + maxW, x > bounds.minX {
                x = bounds.minX
                y += rowH + spacing
                rowH = 0
            }
            sv.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(width: s.width, height: s.height))
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
    }
}
