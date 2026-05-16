import SwiftUI
import AppKit
import Foundation

// MARK: - Capture-day fetcher

struct CaptureDaysSummary {
    let days: Set<Date>          // local start-of-day for every date with captures
    let earliest: Date?          // first capture wall-clock (local)
    let latest: Date?            // most recent capture wall-clock (local)
}

enum CaptureDaysFetchError: Error, LocalizedError {
    case noServerConfigured
    case requestFailed(String)
    case unauthorized
    case decodeFailed

    var errorDescription: String? {
        switch self {
        case .noServerConfigured: return "No backend configured."
        case .requestFailed(let m): return m
        case .unauthorized: return "Backend rejected the request (auth)."
        case .decodeFailed: return "Backend returned an unreadable response."
        }
    }
}

/// Hits `/api/capture_days` with the user's tz offset so the returned
/// dates match the user's local calendar (no UTC drift at midnight).
@MainActor
func fetchCaptureDays(
    config: ConfigManager,
    since: Date,
    until: Date
) async -> Result<CaptureDaysSummary, CaptureDaysFetchError> {
    let serverURL = config.effectiveOwnServerURL
    let candidates = config.ownActivityPortCandidates()
    guard !serverURL.isEmpty,
          let wsURL = URL(string: serverURL),
          let host = wsURL.host
    else { return .failure(.noServerConfigured) }

    let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime]
    let sinceStr = iso.string(from: since)
    let untilStr = iso.string(from: until)
    let tzOffset = TimeZone.current.secondsFromGMT(for: since)

    var lastError: CaptureDaysFetchError = .requestFailed("No reachable activity port.")
    for port in candidates {
        let portSegment = port.isEmpty ? "" : ":\(port)"
        let urlString = "\(scheme)://\(host)\(portSegment)/api/capture_days"
            + "?since=\(sinceStr)&until=\(untilStr)&tz_offset_seconds=\(tzOffset)"
        guard let url = URL(string: urlString) else { continue }

        var request = URLRequest(url: url)
        request.timeoutInterval = 10.0
        if let auth = config.signRequest() {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                lastError = .requestFailed("No response from \(urlString)")
                continue
            }
            if http.statusCode == 401 || http.statusCode == 403 {
                return .failure(.unauthorized)
            }
            if http.statusCode == 404 {
                lastError = .requestFailed("Backend has no /api/capture_days — redeploy needed.")
                continue
            }
            if http.statusCode != 200 {
                lastError = .requestFailed("HTTP \(http.statusCode) from capture_days")
                continue
            }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                lastError = .decodeFailed
                continue
            }
            let rawDays = json["days"] as? [String] ?? []
            let earliestMs = json["earliest_ts_ms"] as? Int
            let latestMs = json["latest_ts_ms"] as? Int

            let cal = Calendar.current
            let parser = DateFormatter()
            parser.dateFormat = "yyyy-MM-dd"
            parser.timeZone = TimeZone.current
            parser.locale = Locale(identifier: "en_US_POSIX")

            var daysSet = Set<Date>()
            for s in rawDays {
                if let d = parser.date(from: s) {
                    daysSet.insert(cal.startOfDay(for: d))
                }
            }
            let earliest = earliestMs.map { Date(timeIntervalSince1970: TimeInterval($0) / 1000.0) }
            let latest = latestMs.map { Date(timeIntervalSince1970: TimeInterval($0) / 1000.0) }
            return .success(CaptureDaysSummary(
                days: daysSet, earliest: earliest, latest: latest
            ))
        } catch {
            lastError = .requestFailed(error.localizedDescription)
            continue
        }
    }
    return .failure(lastError)
}

// MARK: - Calendar grid view

/// Self-contained calendar popover: month grid with a small dot under
/// every day that has captures, prev/next month chevrons, a footer
/// showing earliest capture, and a quick "jump to earliest" button.
///
/// Owns its own monthAnchor + capture-day cache so the Daily Card and
/// Rewind popovers can both drop it in without thinking about state.
struct CapturesCalendarView: View {
    let config: ConfigManager
    @Binding var selectedDate: Date    // start of day in user's local zone
    @Binding var isOpen: Bool

    @State private var monthAnchor: Date
    @State private var capturesByMonth: [Date: Set<Date>] = [:]
    @State private var loadingMonths: Set<Date> = []
    @State private var earliest: Date?
    @State private var latest: Date?
    @State private var loadError: String?

    init(config: ConfigManager, selectedDate: Binding<Date>, isOpen: Binding<Bool>) {
        self.config = config
        self._selectedDate = selectedDate
        self._isOpen = isOpen
        let cal = Calendar.current
        self._monthAnchor = State(initialValue: cal.startOfMonthLocal(for: selectedDate.wrappedValue))
    }

    private let cal = Calendar.current
    private var today: Date { cal.startOfDay(for: Date()) }
    private var thisMonth: Date { cal.startOfMonthLocal(for: today) }

    private var monthLabel: String {
        let f = DateFormatter()
        f.dateFormat = "LLLL yyyy"
        f.locale = Locale.current
        return f.string(from: monthAnchor)
    }

    /// Weekday labels rotated to honor the locale's firstWeekday.
    private var weekdayLabels: [String] {
        let symbols = cal.veryShortStandaloneWeekdaySymbols
        let first = cal.firstWeekday - 1 // 0-based
        guard symbols.indices.contains(first) else { return symbols }
        return Array(symbols[first...] + symbols[..<first])
    }

    /// 42-cell month grid (6 rows × 7 cols), with nils for leading/trailing pad.
    private var monthCells: [Date?] {
        let range = cal.range(of: .day, in: .month, for: monthAnchor) ?? 1..<2
        let firstOfMonth = cal.startOfMonthLocal(for: monthAnchor)
        let firstWeekday = cal.component(.weekday, from: firstOfMonth)
        let leadingNils = (firstWeekday - cal.firstWeekday + 7) % 7
        var cells: [Date?] = Array(repeating: nil, count: leadingNils)
        for offset in 0..<range.count {
            cells.append(cal.date(byAdding: .day, value: offset, to: firstOfMonth))
        }
        while cells.count < 42 {
            cells.append(nil)
        }
        return cells
    }

    private var capturesForMonth: Set<Date> {
        capturesByMonth[monthAnchor] ?? []
    }

    private func canSelect(_ date: Date) -> Bool {
        let day = cal.startOfDay(for: date)
        if day > today { return false }
        if let e = earliest, day < cal.startOfDay(for: e) { return false }
        return true
    }

    private var canShiftForward: Bool {
        monthAnchor < thisMonth
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            weekdayRow
            grid
            Divider().opacity(0.4)
            footer
        }
        .padding(12)
        .frame(width: 280)
        .onAppear {
            fetchMonthIfNeeded(monthAnchor)
        }
        .onChange(of: monthAnchor) { _, newValue in
            fetchMonthIfNeeded(newValue)
        }
    }

    private var header: some View {
        HStack {
            Button {
                shiftMonth(by: -1)
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.borderless)
            .help("Previous month")

            Spacer()

            Text(monthLabel)
                .font(.system(size: 13, weight: .semibold))

            Spacer()

            Button {
                shiftMonth(by: 1)
            } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.borderless)
            .disabled(!canShiftForward)
            .help(canShiftForward ? "Next month" : "Already on current month")

            if loadingMonths.contains(monthAnchor) {
                ProgressView().controlSize(.small)
            }
        }
    }

    private var weekdayRow: some View {
        HStack(spacing: 0) {
            ForEach(weekdayLabels, id: \.self) { label in
                Text(label.uppercased())
                    .font(.system(size: 9, weight: .semibold))
                    .tracking(0.5)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var grid: some View {
        LazyVGrid(
            columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 7),
            spacing: 4
        ) {
            ForEach(0..<42, id: \.self) { idx in
                cell(for: monthCells[idx])
            }
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let err = loadError {
                Text(err)
                    .font(.system(size: 10))
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            }
            HStack(spacing: 6) {
                if let e = earliest {
                    Text("Earliest: \(fmtShort(e))")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                } else {
                    Text("No captures yet.")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                Spacer()
                Button("Jump to earliest") {
                    guard let e = earliest else { return }
                    selectedDate = cal.startOfDay(for: e)
                    monthAnchor = cal.startOfMonthLocal(for: e)
                    isOpen = false
                }
                .buttonStyle(.borderless)
                .font(.system(size: 10))
                .disabled(earliest == nil)
            }
        }
    }

    @ViewBuilder
    private func cell(for date: Date?) -> some View {
        if let date {
            let dayStart = cal.startOfDay(for: date)
            let isSelected = cal.isDate(date, inSameDayAs: selectedDate)
            let isToday = cal.isDateInToday(date)
            let hasCaptures = capturesForMonth.contains(dayStart)
            let selectable = canSelect(date)

            Button {
                if selectable {
                    selectedDate = dayStart
                    isOpen = false
                }
            } label: {
                VStack(spacing: 2) {
                    Text("\(cal.component(.day, from: date))")
                        .font(.system(
                            size: 12,
                            weight: isSelected || isToday ? .bold : .regular,
                            design: .default
                        ))
                        .foregroundStyle(selectable ? .primary : .tertiary)
                    Circle()
                        .fill(hasCaptures ? Color.green : Color.clear)
                        .frame(width: 4, height: 4)
                }
                .frame(maxWidth: .infinity, minHeight: 30)
                .background(
                    RoundedRectangle(cornerRadius: 4)
                        .fill(isSelected
                              ? Color.accentColor.opacity(0.30)
                              : (isToday ? Color.white.opacity(0.08) : .clear))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 4)
                        .stroke(
                            isToday ? Color.accentColor.opacity(0.4) : .clear,
                            lineWidth: 1
                        )
                )
            }
            .buttonStyle(.plain)
            .disabled(!selectable)
            .help(hasCaptures ? "\(fmtFull(date)) · has captures" : fmtFull(date))
        } else {
            Color.clear.frame(minHeight: 30)
        }
    }

    private func shiftMonth(by months: Int) {
        guard let next = cal.date(byAdding: .month, value: months, to: monthAnchor) else { return }
        let nextStart = cal.startOfMonthLocal(for: next)
        if months > 0, nextStart > thisMonth { return }
        monthAnchor = nextStart
    }

    private func fetchMonthIfNeeded(_ month: Date) {
        let anchor = cal.startOfMonthLocal(for: month)
        guard capturesByMonth[anchor] == nil, !loadingMonths.contains(anchor) else { return }
        guard let end = cal.date(byAdding: .month, value: 1, to: anchor) else { return }

        loadingMonths.insert(anchor)
        loadError = nil
        Task {
            let result = await fetchCaptureDays(config: config, since: anchor, until: end)
            await MainActor.run {
                loadingMonths.remove(anchor)
                switch result {
                case .success(let summary):
                    capturesByMonth[anchor] = summary.days
                    // earliest/latest are tenant-wide, not month-scoped, so
                    // first response is enough — but updating each time is
                    // also harmless and keeps them fresh.
                    if let e = summary.earliest { earliest = e }
                    if let l = summary.latest { latest = l }
                case .failure(let err):
                    loadError = err.localizedDescription
                }
            }
        }
    }

    private func fmtShort(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "MMM d"
        f.locale = Locale.current
        return f.string(from: d)
    }

    private func fmtFull(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEEE MMM d, yyyy"
        f.locale = Locale.current
        return f.string(from: d)
    }
}

// MARK: - Calendar helper

extension Calendar {
    /// Start-of-month (00:00 local) for the month containing `date`.
    /// Plain `dateComponents([.year, .month])` returns a UTC midnight
    /// which can be the previous day in negative-offset locales — use
    /// `startOfDay(for: firstOfMonth)` to anchor to the user's zone.
    func startOfMonthLocal(for date: Date) -> Date {
        let comps = dateComponents([.year, .month], from: date)
        guard let first = self.date(from: comps) else { return date }
        return startOfDay(for: first)
    }
}
