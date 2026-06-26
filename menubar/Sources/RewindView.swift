import SwiftUI
import AppKit
import Foundation

// MARK: - Dedicated URLSession for Rewind

/// Image fetches are bursty (16+ prefetches in flight during play). The
/// shared URLSession caps at 6 connections per host which queues most of
/// the burst. This session raises that cap to 12 and disables disk cache
/// (we cache decoded NSImages in-process, not bytes).
enum RewindNetworking {
    static let imageSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.httpMaximumConnectionsPerHost = 12
        cfg.timeoutIntervalForRequest = 15.0
        cfg.timeoutIntervalForResource = 30.0
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        return URLSession(configuration: cfg)
    }()

    static let indexSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.httpMaximumConnectionsPerHost = 4
        cfg.timeoutIntervalForRequest = 25.0
        cfg.timeoutIntervalForResource = 60.0
        return URLSession(configuration: cfg)
    }()
}

// MARK: - Frame index

struct FrameRef: Identifiable, Equatable, Hashable {
    let id: Int        // backend frame id
    let timestamp: Date
}

enum RewindFetchError: Error, LocalizedError {
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

@MainActor
func fetchFrameIndex(
    config: ConfigManager,
    since: Date,
    until: Date
) async -> Result<[FrameRef], RewindFetchError> {
    let session = RewindNetworking.indexSession
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

    var lastError: RewindFetchError = .requestFailed("No reachable activity port.")
    for port in candidates {
        let portSegment = port.isEmpty ? "" : ":\(port)"
        let urlString = "\(scheme)://\(host)\(portSegment)/api/frame_index"
            + "?since=\(sinceStr)&until=\(untilStr)"
        guard let url = URL(string: urlString) else { continue }

        var request = URLRequest(url: url)
        request.timeoutInterval = 20.0
        if let auth = config.signRequest() {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                lastError = .requestFailed("No response from \(urlString)")
                continue
            }
            if http.statusCode == 401 || http.statusCode == 403 {
                return .failure(.unauthorized)
            }
            if http.statusCode == 404 {
                // Older backend without /api/frame_index — surface a
                // distinct error so the UI can prompt for redeploy.
                lastError = .requestFailed(
                    "Backend has no /api/frame_index — redeploy needed."
                )
                continue
            }
            if http.statusCode != 200 {
                lastError = .requestFailed("HTTP \(http.statusCode) from frame index")
                continue
            }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let raw = json["frames"] as? [[String: Any]]
            else {
                lastError = .decodeFailed
                continue
            }
            let frames: [FrameRef] = raw.compactMap { entry in
                guard let id = entry["id"] as? Int,
                      let tsMs = entry["ts_ms"] as? Int
                else { return nil }
                return FrameRef(
                    id: id,
                    timestamp: Date(timeIntervalSince1970: TimeInterval(tsMs) / 1000.0)
                )
            }
            // Defensive: client-side filter in case an older backend
            // returns the full recent list.
            let filtered = frames.filter { $0.timestamp >= since && $0.timestamp < until }
                .sorted(by: { $0.timestamp < $1.timestamp })
            return .success(filtered)
        } catch {
            lastError = .requestFailed(error.localizedDescription)
            continue
        }
    }
    return .failure(lastError)
}

struct ScreenshotResult {
    let image: NSImage
    let app: String?
    let window: String?
}

struct ThumbnailRef: Identifiable {
    let id: Int
    let tsMs: Int
    let image: NSImage
}

struct TranscriptRef: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let transcript: String
    let title: String?
    let durationSeconds: Double?
    let sourceAudioPath: String?
    let sourceTranscriptPath: String?

    var durationText: String {
        guard let durationSeconds else { return "" }
        let seconds = max(0, Int(durationSeconds.rounded()))
        let h = seconds / 3600
        let m = (seconds % 3600) / 60
        let s = seconds % 60
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m \(s)s" }
        return "\(s)s"
    }
}

private enum TranscriptScope: String, CaseIterable, Identifiable {
    case day = "Day"
    case all = "All"

    var id: String { rawValue }
}

private struct TranscriptHealth {
    var inboxAudio: Int = 0
    var transcripts: Int = 0
    var failed: Int = 0
    var pending: Int = 0
    var partials: Int = 0
    var lockAgeSeconds: Double?
    var lockPID: Int?
    var latestTranscript: Date?

    var active: Bool {
        if partials > 0 { return true }
        if let lockAgeSeconds { return lockAgeSeconds < 30 * 60 }
        return false
    }

    var statusText: String {
        if active { return "active" }
        if pending > 0 { return "waiting" }
        return "caught up"
    }

    var statusColor: Color {
        if failed > 0 && pending == 0 { return .orange }
        if active { return .green }
        if pending > 0 { return .yellow }
        return .secondary
    }
}

/// Bulk-fetch all thumbnails for a day so the scrubber renders instantly
/// regardless of network. Server returns up to 5000 entries in one shot;
/// payload is typically 5–15MB for a full day of capture.
@MainActor
func fetchThumbnails(
    config: ConfigManager,
    since: Date,
    until: Date
) async -> Result<[ThumbnailRef], RewindFetchError> {
    let session = RewindNetworking.indexSession
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

    var lastError: RewindFetchError = .requestFailed("No reachable activity port.")
    for port in candidates {
        let portSegment = port.isEmpty ? "" : ":\(port)"
        let urlString = "\(scheme)://\(host)\(portSegment)/api/thumbnails"
            + "?since=\(sinceStr)&until=\(untilStr)"
        guard let url = URL(string: urlString) else { continue }

        var request = URLRequest(url: url)
        request.timeoutInterval = 60.0
        if let auth = config.signRequest() {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                lastError = .requestFailed("No response from \(urlString)")
                continue
            }
            if http.statusCode == 401 || http.statusCode == 403 {
                return .failure(.unauthorized)
            }
            if http.statusCode == 404 {
                lastError = .requestFailed(
                    "Backend has no /api/thumbnails — redeploy needed."
                )
                continue
            }
            if http.statusCode != 200 {
                lastError = .requestFailed("HTTP \(http.statusCode) from thumbnails")
                continue
            }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let raw = json["thumbnails"] as? [[String: Any]]
            else {
                lastError = .decodeFailed
                continue
            }
            let thumbs: [ThumbnailRef] = raw.compactMap { entry in
                guard let id = entry["id"] as? Int,
                      let tsMs = entry["ts_ms"] as? Int,
                      let b64 = entry["thumb_b64"] as? String,
                      let bytes = Data(base64Encoded: b64),
                      let img = NSImage(data: bytes)
                else { return nil }
                return ThumbnailRef(id: id, tsMs: tsMs, image: img)
            }
            return .success(thumbs)
        } catch {
            lastError = .requestFailed(error.localizedDescription)
            continue
        }
    }
    return .failure(lastError)
}

@MainActor
func fetchVoiceMemoTranscripts(
    config: ConfigManager,
    since: Date? = nil,
    until: Date? = nil,
    search: String? = nil
) async -> Result<[TranscriptRef], RewindFetchError> {
    let session = RewindNetworking.indexSession
    let controlPort = config.controlPort.trimmingCharacters(in: .whitespacesAndNewlines)
    let port = controlPort.isEmpty ? "7892" : controlPort
    let encodedApp = "voice_memo_life"
    var localURLString = "http://127.0.0.1:\(port)/transcripts"
        + "?meeting_app=\(encodedApp)&limit=2000"
    if let since {
        localURLString += "&since=\(since.timeIntervalSince1970)"
    }
    if let until {
        localURLString += "&until=\(until.timeIntervalSince1970)"
    }
    if let search, !search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
       let escaped = search.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
        localURLString += "&search=\(escaped)"
    }

    if let url = URL(string: localURLString) {
        do {
            let (data, response) = try await session.data(from: url)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return decodeTranscripts(data)
            }
        } catch {
            // Fall through to backend /api/transcripts below. The local
            // control server can be briefly unavailable during daemon restarts.
        }
    }

    let serverURL = config.effectiveOwnServerURL
    let candidates = config.ownActivityPortCandidates()
    guard !serverURL.isEmpty,
          let wsURL = URL(string: serverURL),
          let host = wsURL.host
    else { return .failure(.noServerConfigured) }

    let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime]

    var lastError: RewindFetchError = .requestFailed("No reachable transcript source.")
    for port in candidates {
        let portSegment = port.isEmpty ? "" : ":\(port)"
        var urlString = "\(scheme)://\(host)\(portSegment)/api/transcripts"
            + "?meeting_app=\(encodedApp)&limit=2000"
        if let since {
            urlString += "&since=\(iso.string(from: since))"
        }
        if let until {
            urlString += "&until=\(iso.string(from: until))"
        }
        if let search, !search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           let escaped = search.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            urlString += "&search=\(escaped)"
        }
        guard let url = URL(string: urlString) else { continue }

        var request = URLRequest(url: url)
        request.timeoutInterval = 20.0
        if let auth = config.signRequest() {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                lastError = .requestFailed("No response from transcript source")
                continue
            }
            if http.statusCode == 401 || http.statusCode == 403 {
                return .failure(.unauthorized)
            }
            if http.statusCode != 200 {
                lastError = .requestFailed("HTTP \(http.statusCode) from transcripts")
                continue
            }
            return decodeTranscripts(data)
        } catch {
            lastError = .requestFailed(error.localizedDescription)
            continue
        }
    }
    return .failure(lastError)
}

private func decodeTranscripts(_ data: Data) -> Result<[TranscriptRef], RewindFetchError> {
    guard let raw = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
        return .failure(.decodeFailed)
    }
    let rows: [TranscriptRef] = raw.compactMap { entry in
        let tsSeconds: Double
        if let ts = entry["ts"] as? Double {
            tsSeconds = ts
        } else if let ts = entry["ts"] as? Int {
            tsSeconds = Double(ts)
        } else if let tsMs = entry["ts_ms"] as? Int {
            tsSeconds = Double(tsMs) / 1000.0
        } else {
            return nil
        }
        guard let text = entry["transcript"] as? String,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }
        let source = entry["source_transcript_path"] as? String
        let id = source ?? "\(Int(tsSeconds * 1000))-\(text.hashValue)"
        return TranscriptRef(
            id: id,
            timestamp: Date(timeIntervalSince1970: tsSeconds),
            transcript: text,
            title: entry["title"] as? String,
            durationSeconds: entry["duration_s"] as? Double,
            sourceAudioPath: entry["source_audio_path"] as? String,
            sourceTranscriptPath: source
        )
    }
    return .success(rows.sorted(by: { $0.timestamp > $1.timestamp }))
}

@MainActor
func fetchScreenshot(
    config: ConfigManager,
    frameId: Int
) async -> Result<ScreenshotResult, RewindFetchError> {
    let session = RewindNetworking.imageSession
    let serverURL = config.effectiveOwnServerURL
    let candidates = config.ownActivityPortCandidates()
    guard !serverURL.isEmpty,
          let wsURL = URL(string: serverURL),
          let host = wsURL.host
    else { return .failure(.noServerConfigured) }

    let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"

    var lastError: RewindFetchError = .requestFailed("No reachable activity port.")
    for port in candidates {
        let portSegment = port.isEmpty ? "" : ":\(port)"
        let urlString = "\(scheme)://\(host)\(portSegment)/api/screenshot?frame_id=\(frameId)"
        guard let url = URL(string: urlString) else { continue }

        var request = URLRequest(url: url)
        request.timeoutInterval = 15.0
        if let auth = config.signRequest() {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                lastError = .requestFailed("No response from \(urlString)")
                continue
            }
            if http.statusCode == 401 || http.statusCode == 403 {
                return .failure(.unauthorized)
            }
            if http.statusCode == 404 {
                lastError = .requestFailed("Frame \(frameId) missing on backend.")
                continue
            }
            if http.statusCode != 200 {
                lastError = .requestFailed("HTTP \(http.statusCode) from screenshot")
                continue
            }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let b64 = json["image_b64"] as? String,
                  let imageData = Data(base64Encoded: b64),
                  let nsImage = NSImage(data: imageData)
            else {
                lastError = .decodeFailed
                continue
            }
            let frame = json["frame"] as? [String: Any]
            let app = frame?["app"] as? String
            let window = frame?["window"] as? String
            return .success(ScreenshotResult(image: nsImage, app: app, window: window))
        } catch {
            lastError = .requestFailed(error.localizedDescription)
            continue
        }
    }
    return .failure(lastError)
}

// MARK: - Rewind window

private let kPlaybackBaseInterval: Double = 0.15   // seconds per index advance @ 1x

struct RewindWindowView: View {
    let state: AppState
    let config: ConfigManager
    /// Owned by AppDelegate, watched here so "click Rewind from a Daily
    /// Card opened to May 11" actually navigates to May 11 even when a
    /// Rewind window is already on screen showing a different day.
    let coordinator: RewindCoordinator

    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @State private var calendarOpen: Bool = false
    @State private var indexByDay: [Date: [FrameRef]] = [:]
    @State private var loadingIndexFor: Set<Date> = []
    @State private var indexError: String?

    @State private var positionByDay: [Date: Double] = [:]
    @State private var currentImage: NSImage?
    @State private var currentMeta: (app: String?, window: String?)?
    @State private var currentLoadedFrameId: Int?
    @State private var imageError: String?

    @State private var imageCache: [Int: ScreenshotResult] = [:]
    @State private var inFlightFetches: Set<Int> = []

    // Pre-computed thumbnails (256px max, ~5KB each). Loaded in bulk on
    // day open so the scrubber renders instantly even before any full-res
    // image has been fetched. The full-res JPEG is layered on top once it
    // arrives, so the user always sees something — never a black frame.
    @State private var thumbsByDay: [Date: [Int: NSImage]] = [:]
    @State private var loadingThumbsFor: Set<Date> = []
    @State private var thumbsError: String?

    @State private var transcriptPanelOpen: Bool = false
    @State private var transcriptScope: TranscriptScope = .day
    @State private var transcriptsByDay: [Date: [TranscriptRef]] = [:]
    @State private var loadingTranscriptsFor: Set<Date> = []
    @State private var globalTranscripts: [TranscriptRef] = []
    @State private var loadingGlobalTranscripts: Bool = false
    @State private var transcriptsError: String?
    @State private var globalTranscriptsError: String?
    @State private var transcriptSearch: String = ""
    @State private var transcriptHealth: TranscriptHealth = .init()

    @State private var playing = false
    @State private var playSpeed: Double = 5.0
    @State private var playTask: Task<Void, Never>?

    private var today: Date { Calendar.current.startOfDay(for: Date()) }
    private var isToday: Bool { selectedDate == today }
    private var canGoForward: Bool { selectedDate < today }

    private var frames: [FrameRef] { indexByDay[selectedDate] ?? [] }
    private var currentPosition: Double {
        positionByDay[selectedDate] ?? 0
    }
    private var currentIndex: Int {
        let clamped = max(0.0, min(currentPosition, Double(max(0, frames.count - 1))))
        return Int(clamped.rounded())
    }
    private var currentFrame: FrameRef? {
        frames.indices.contains(currentIndex) ? frames[currentIndex] : nil
    }

    private var currentThumb: NSImage? {
        guard let f = currentFrame else { return nil }
        return thumbsByDay[selectedDate]?[f.id]
    }

    private var transcripts: [TranscriptRef] {
        transcriptsByDay[selectedDate] ?? []
    }

    private var filteredTranscripts: [TranscriptRef] {
        let needle = transcriptSearch.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return transcripts }
        return transcripts.filter {
            $0.transcript.localizedCaseInsensitiveContains(needle)
            || ($0.title?.localizedCaseInsensitiveContains(needle) ?? false)
        }
    }

    private var shownTranscripts: [TranscriptRef] {
        transcriptScope == .all ? globalTranscripts : filteredTranscripts
    }

    private var sourceTranscriptCount: Int {
        transcriptScope == .all ? globalTranscripts.count : transcripts.count
    }

    private var transcriptsLoading: Bool {
        transcriptScope == .all
            ? loadingGlobalTranscripts
            : loadingTranscriptsFor.contains(selectedDate)
    }

    private var activeTranscriptsError: String? {
        transcriptScope == .all ? globalTranscriptsError : transcriptsError
    }

    /// True when the currently displayed *full-res* bitmap matches the
    /// scrubber position. The thumbnail (if present) doesn't count as a
    /// "match" — it's a stand-in until the full-res arrives.
    private var fullResMatchesCurrent: Bool {
        guard let f = currentFrame else { return true }
        return currentLoadedFrameId == f.id
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

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 10) {
                navHeader
                Divider().opacity(0.4)
                framePreview
                scrubber
                controls
            }
            if transcriptPanelOpen {
                Divider().opacity(0.45)
                transcriptPanel
                    .frame(width: 340)
            }
        }
        .onAppear {
            // First display: honor whichever day the open-call requested.
            selectedDate = coordinator.requestedDate
            fetchIndexIfNeeded(selectedDate)
            fetchThumbsIfNeeded(selectedDate)
            loadFrameIfNeeded()
        }
        .onChange(of: coordinator.requestId) { _, _ in
            // Re-opening Rewind from a different Daily Card snaps the
            // view to that day even if we're already on screen.
            selectedDate = coordinator.requestedDate
        }
        .onChange(of: selectedDate) { _, _ in
            stopPlay()
            fetchIndexIfNeeded(selectedDate)
            fetchThumbsIfNeeded(selectedDate)
            if transcriptPanelOpen { fetchTranscriptsIfNeeded(selectedDate) }
            currentImage = nil
            currentMeta = nil
            currentLoadedFrameId = nil
            loadFrameIfNeeded()
        }
        .onChange(of: transcriptPanelOpen) { _, open in
            if open {
                refreshTranscriptHealth()
                loadVisibleTranscripts(force: false)
            }
        }
        .onChange(of: transcriptScope) { _, _ in
            if transcriptPanelOpen { loadVisibleTranscripts(force: false) }
        }
        .onChange(of: currentIndex) { _, _ in
            loadFrameIfNeeded()
            prefetch(around: currentIndex)
        }
        .onDisappear {
            stopPlay()
        }
    }

    // MARK: navigation

    private var navHeader: some View {
        HStack(spacing: 8) {
            Button { shiftDay(by: -1) } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .help("Previous day")

            Button {
                calendarOpen.toggle()
            } label: {
                HStack(spacing: 4) {
                    Text(dateLabel)
                        .font(.system(size: 12, weight: .medium))
                    Image(systemName: "calendar")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                .frame(minWidth: 200, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Pick a date")
            .popover(isPresented: $calendarOpen) {
                CapturesCalendarView(
                    config: config,
                    selectedDate: $selectedDate,
                    isOpen: $calendarOpen
                )
                .preferredColorScheme(.dark)
            }

            Button { shiftDay(by: 1) } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(!canGoForward)
            .help(canGoForward ? "Next day" : "Already on today")

            if loadingIndexFor.contains(selectedDate) {
                ProgressView().controlSize(.small)
            }

            Spacer()

            if !isToday {
                Button("Today") { selectedDate = today }
                    .buttonStyle(.bordered).controlSize(.small)
            }

            Button {
                indexByDay[selectedDate] = nil
                thumbsByDay[selectedDate] = nil
                fetchIndexIfNeeded(selectedDate)
                fetchThumbsIfNeeded(selectedDate)
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .help("Refresh this day's index + thumbs")

            Button {
                transcriptPanelOpen.toggle()
                if transcriptPanelOpen { fetchTranscriptsIfNeeded(selectedDate, force: true) }
            } label: {
                Image(systemName: "quote.bubble")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .tint(transcriptPanelOpen ? .accentColor : .clear)
            .controlSize(.small)
            .help("Show voice memo transcripts for this day")
        }
    }

    private func shiftDay(by days: Int) {
        guard let next = Calendar.current.date(byAdding: .day, value: days, to: selectedDate)
        else { return }
        let nextStart = Calendar.current.startOfDay(for: next)
        if nextStart > today { return }
        selectedDate = nextStart
    }

    // MARK: preview

    private var framePreview: some View {
        ZStack {
            // Base layer: black background
            Color.black

            // Layer 1: thumbnail (low-res but instant). Always present
            // once thumbs for the day have loaded — gives the scrubber
            // an immediate response while full-res is in flight.
            if let thumb = currentThumb {
                Image(nsImage: thumb)
                    .resizable()
                    .interpolation(.medium)
                    .scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            // Layer 2: full-res image, covers the thumb when ready.
            if let img = currentImage, fullResMatchesCurrent {
                Image(nsImage: img)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let err = imageError, currentThumb == nil {
                VStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 18))
                        .foregroundStyle(.orange)
                    Text(err)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if frames.isEmpty {
                if loadingIndexFor.contains(selectedDate) {
                    ProgressView().controlSize(.small)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let err = indexError {
                    VStack(spacing: 6) {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.system(size: 18))
                            .foregroundStyle(.orange)
                        Text(err)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal, 24)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    Text(isToday
                        ? "No frames captured today yet."
                        : "No frames recorded on this day.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else if currentFrame != nil && currentImage == nil && currentThumb == nil {
                ProgressView().controlSize(.small)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            // Top-right loading badge: full-res for the current scrubber
            // position is still in flight. We hide it once the thumb is
            // up (visual signal of "loading" is unnecessary when there's
            // already a usable image showing).
            VStack {
                HStack {
                    Spacer()
                    if !fullResMatchesCurrent && currentThumb == nil {
                        ZStack {
                            Circle()
                                .fill(Color.black.opacity(0.6))
                                .frame(width: 28, height: 28)
                            ProgressView().controlSize(.small)
                        }
                        .padding(8)
                    }
                }
                Spacer()
            }
            .allowsHitTesting(false)

            // Bottom overlay: timestamp + app + window
            if let f = currentFrame {
                VStack {
                    Spacer()
                    HStack(spacing: 6) {
                        Text(fmtClock(f.timestamp))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(fullResMatchesCurrent ? .white : .white.opacity(0.55))
                        if let meta = currentMeta {
                            if let app = meta.app, !app.isEmpty {
                                Text(app)
                                    .font(.system(size: 11, weight: .medium))
                                    .foregroundStyle(.white.opacity(0.85))
                            }
                            if let window = meta.window, !window.isEmpty {
                                Text("· \(window)")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.white.opacity(0.6))
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                            }
                        }
                        Spacer(minLength: 0)
                        if !fullResMatchesCurrent {
                            // Subtle "still loading full-res" cue. Thumb
                            // is already visible — this is just so the
                            // user knows higher quality is incoming.
                            Text(currentThumb == nil ? "loading…" : "preview")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.white.opacity(0.65))
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Color.black.opacity(0.55))
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .frame(minHeight: 320)
    }

    // MARK: scrubber

    private var scrubber: some View {
        HStack(spacing: 8) {
            if !frames.isEmpty {
                Text("\(currentIndex + 1)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .trailing)
                Slider(
                    value: Binding(
                        get: { currentPosition },
                        set: { newValue in
                            positionByDay[selectedDate] = newValue
                        }
                    ),
                    in: 0...Double(max(1, frames.count - 1)),
                    step: 1
                )
                Text("\(frames.count)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .leading)
            } else {
                Slider(value: .constant(0.0), in: 0...1).disabled(true)
            }
        }
    }

    // MARK: playback controls

    private var controls: some View {
        HStack(spacing: 6) {
            Button { jumpBy(-1) } label: {
                Image(systemName: "backward.frame.fill")
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(frames.isEmpty)
            .help("Previous frame")

            Button { playing ? stopPlay() : startPlay() } label: {
                Image(systemName: playing ? "pause.fill" : "play.fill")
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.borderedProminent).controlSize(.small)
            .disabled(frames.isEmpty)
            .help(playing ? "Pause" : "Play")

            Button { jumpBy(1) } label: {
                Image(systemName: "forward.frame.fill")
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(frames.isEmpty)
            .help("Next frame")

            Divider().frame(height: 16).padding(.horizontal, 2)

            Picker("", selection: $playSpeed) {
                Text("1x").tag(1.0)
                Text("2x").tag(2.0)
                Text("5x").tag(5.0)
                Text("10x").tag(10.0)
            }
            .pickerStyle(.segmented)
            .frame(width: 180)
            .labelsHidden()

            Spacer()

            Text(statusLine)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }

    private var statusLine: String {
        if loadingIndexFor.contains(selectedDate) { return "indexing…" }
        if frames.isEmpty { return "0 frames" }
        let thumbsLoading = loadingThumbsFor.contains(selectedDate)
        let thumbsReady = (thumbsByDay[selectedDate]?.count ?? 0)
        let suffix: String
        if thumbsLoading {
            suffix = " · loading thumbs"
        } else if thumbsReady > 0 {
            suffix = " · \(thumbsReady) thumbs"
        } else if thumbsError != nil {
            suffix = " · thumbs unavailable"
        } else {
            suffix = ""
        }
        return "\(frames.count) frames · \(fmtClock(frames.first!.timestamp)) → \(fmtClock(frames.last!.timestamp))\(suffix)"
    }

    private var transcriptPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "quote.bubble")
                    .font(.system(size: 12, weight: .semibold))
                Text("Voice Transcripts")
                    .font(.system(size: 12, weight: .semibold))
                Spacer()
                if transcriptsLoading {
                    ProgressView().controlSize(.small)
                }
                Button {
                    refreshTranscriptHealth()
                    loadVisibleTranscripts(force: true)
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Refresh transcripts")
            }

            Picker("Transcript scope", selection: $transcriptScope) {
                ForEach(TranscriptScope.allCases) { scope in
                    Text(scope.rawValue).tag(scope)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            transcriptHealthView

            HStack(spacing: 6) {
                TextField(
                    transcriptScope == .all ? "Search all transcripts" : "Search this day",
                    text: $transcriptSearch
                )
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11))
                .onSubmit {
                    if transcriptScope == .all { fetchGlobalTranscripts(force: true) }
                }

                if transcriptScope == .all {
                    Button {
                        fetchGlobalTranscripts(force: true)
                    } label: {
                        Image(systemName: "magnifyingglass")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(loadingGlobalTranscripts)
                    .help("Search all voice memo transcripts")
                }
            }

            HStack(spacing: 6) {
                Text(transcriptCountLabel)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
                Spacer()
                Button(transcriptScope == .all ? "Copy Results" : "Copy Day") {
                    copyToPasteboard(shownTranscripts.map { transcriptCopyBlock($0) }.joined(separator: "\n\n"))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(shownTranscripts.isEmpty)
            }

            if let err = activeTranscriptsError, shownTranscripts.isEmpty {
                Text(err)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 8)
            } else if transcriptsLoading && shownTranscripts.isEmpty {
                Spacer()
                ProgressView()
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                Spacer()
            } else if shownTranscripts.isEmpty {
                Text(emptyTranscriptMessage)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 8)
                Spacer()
            } else {
                ScrollView(.vertical, showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(shownTranscripts) { row in
                            transcriptRow(row)
                        }
                    }
                }
            }
        }
    }

    private var transcriptHealthView: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle()
                    .fill(transcriptHealth.statusColor)
                    .frame(width: 7, height: 7)
                Text("Transcription \(transcriptHealth.statusText)")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if let latest = transcriptHealth.latestTranscript {
                    Text("latest \(fmtDateTime(latest))")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }

            HStack(spacing: 8) {
                healthMetric("audio", transcriptHealth.inboxAudio)
                healthMetric("done", transcriptHealth.transcripts)
                healthMetric("pending", transcriptHealth.pending)
                healthMetric("failed", transcriptHealth.failed)
            }

            if transcriptHealth.partials > 0 || transcriptHealth.lockAgeSeconds != nil {
                HStack(spacing: 6) {
                    if transcriptHealth.partials > 0 {
                        Text("\(transcriptHealth.partials) checkpoint\(transcriptHealth.partials == 1 ? "" : "s")")
                    }
                    if let age = transcriptHealth.lockAgeSeconds {
                        Text("lock \(formatAge(age))")
                    }
                    if let pid = transcriptHealth.lockPID {
                        Text("pid \(pid)")
                    }
                    Spacer()
                }
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.tertiary)
                .lineLimit(1)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.045))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func healthMetric(_ label: String, _ value: Int) -> some View {
        HStack(spacing: 3) {
            Text(label)
                .foregroundStyle(.tertiary)
            Text("\(value)")
                .foregroundStyle(.secondary)
        }
        .font(.system(size: 9, design: .monospaced))
        .lineLimit(1)
    }

    private func transcriptRow(_ row: TranscriptRef) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(transcriptScope == .all ? fmtDateTime(row.timestamp) : fmtClock(row.timestamp))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(minWidth: transcriptScope == .all ? 76 : 58, alignment: .leading)
                if let title = row.title, !title.isEmpty {
                    Text(title)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                Spacer(minLength: 0)
                if !row.durationText.isEmpty {
                    Text(row.durationText)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Text(row.transcript)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(transcriptScope == .all ? 5 : 8)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .clipped()
            HStack(spacing: 6) {
                Button("Copy") { copyToPasteboard(transcriptCopyBlock(row)) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                if let path = row.sourceTranscriptPath {
                    Button("Open") { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
                Spacer()
            }
        }
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.black.opacity(0.001))
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Color.white.opacity(0.08))
                .frame(height: 1)
        }
    }

    // MARK: fetching

    private var transcriptCountLabel: String {
        if transcriptScope == .all {
            let needle = transcriptSearch.trimmingCharacters(in: .whitespacesAndNewlines)
            return needle.isEmpty ? "\(globalTranscripts.count) latest" : "\(globalTranscripts.count) matches"
        }
        return "\(filteredTranscripts.count) of \(sourceTranscriptCount)"
    }

    private var emptyTranscriptMessage: String {
        if transcriptScope == .all {
            return transcriptSearch.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "No voice memo transcripts indexed yet."
                : "No matching transcripts across all days."
        }
        return transcriptSearch.isEmpty
            ? "No voice memo transcripts for this day."
            : "No matching transcripts for this day."
    }

    private func loadVisibleTranscripts(force: Bool) {
        if transcriptScope == .all {
            fetchGlobalTranscripts(force: force)
        } else {
            fetchTranscriptsIfNeeded(selectedDate, force: force)
        }
    }

    private func refreshTranscriptHealth() {
        transcriptHealth = Self.readTranscriptHealth()
    }

    private static func readTranscriptHealth() -> TranscriptHealth {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: "/Users/Shared/voice-transcripts", isDirectory: true)
        let inbox = root.appendingPathComponent("inbox", isDirectory: true)
        let transcripts = root.appendingPathComponent("transcripts", isDirectory: true)
        let failed = root.appendingPathComponent("failed", isDirectory: true)
        let partials = root.appendingPathComponent("partials", isDirectory: true)
        let audioExts: Set<String> = ["m4a", "mp4", "qta", "mov", "wav", "mp3", "aac"]

        let inboxAudio = countFiles(in: inbox) { audioExts.contains($0.pathExtension.lowercased()) }
        let transcriptFiles = files(in: transcripts) { $0.pathExtension.lowercased() == "txt" }
        let failedCount = countFiles(in: failed) { $0.pathExtension.lowercased() == "json" }
        let partialCount = countFiles(in: partials) { $0.pathExtension.lowercased() == "json" }

        var latestTranscript: Date?
        for file in transcriptFiles {
            if let attrs = try? fm.attributesOfItem(atPath: file.path),
               let modified = attrs[.modificationDate] as? Date,
               latestTranscript == nil || modified > latestTranscript! {
                latestTranscript = modified
            }
        }

        var lockAge: Double?
        var lockPID: Int?
        let lock = root.appendingPathComponent(".transcribe.lock")
        if let data = try? Data(contentsOf: lock),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let pid = json["pid"] as? Int {
                lockPID = pid
            }
            if let timestamp = json["time"] as? Double {
                lockAge = max(0, Date().timeIntervalSince1970 - timestamp)
            }
        }

        return TranscriptHealth(
            inboxAudio: inboxAudio,
            transcripts: transcriptFiles.count,
            failed: failedCount,
            pending: max(0, inboxAudio - transcriptFiles.count - failedCount),
            partials: partialCount,
            lockAgeSeconds: lockAge,
            lockPID: lockPID,
            latestTranscript: latestTranscript
        )
    }

    private static func files(in directory: URL, matching predicate: (URL) -> Bool) -> [URL] {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else { return [] }
        return urls.filter { url in
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue else {
                return false
            }
            return predicate(url)
        }
    }

    private static func countFiles(in directory: URL, matching predicate: (URL) -> Bool) -> Int {
        files(in: directory, matching: predicate).count
    }

    private func fetchThumbsIfNeeded(_ day: Date) {
        let dayStart = Calendar.current.startOfDay(for: day)
        guard thumbsByDay[dayStart] == nil, !loadingThumbsFor.contains(dayStart) else { return }
        guard let dayEnd = Calendar.current.date(byAdding: .day, value: 1, to: dayStart)
        else { return }

        loadingThumbsFor.insert(dayStart)
        thumbsError = nil
        Task {
            let result = await fetchThumbnails(config: config, since: dayStart, until: dayEnd)
            await MainActor.run {
                loadingThumbsFor.remove(dayStart)
                switch result {
                case .success(let thumbs):
                    var dict: [Int: NSImage] = [:]
                    dict.reserveCapacity(thumbs.count)
                    for t in thumbs {
                        dict[t.id] = t.image
                    }
                    thumbsByDay[dayStart] = dict
                case .failure(let err):
                    thumbsError = err.localizedDescription
                }
            }
        }
    }

    private func fetchIndexIfNeeded(_ day: Date) {
        let dayStart = Calendar.current.startOfDay(for: day)
        guard indexByDay[dayStart] == nil, !loadingIndexFor.contains(dayStart) else { return }
        guard let dayEnd = Calendar.current.date(byAdding: .day, value: 1, to: dayStart)
        else { return }

        loadingIndexFor.insert(dayStart)
        indexError = nil
        Task {
            let result = await fetchFrameIndex(config: config, since: dayStart, until: dayEnd)
            await MainActor.run {
                loadingIndexFor.remove(dayStart)
                switch result {
                case .success(let frames):
                    indexByDay[dayStart] = frames
                    if positionByDay[dayStart] == nil {
                        positionByDay[dayStart] = Double(max(0, frames.count - 1))
                    }
                case .failure(let err):
                    indexError = err.localizedDescription
                }
            }
        }
    }

    private func fetchTranscriptsIfNeeded(_ day: Date, force: Bool = false) {
        let dayStart = Calendar.current.startOfDay(for: day)
        guard force || transcriptsByDay[dayStart] == nil else { return }
        guard !loadingTranscriptsFor.contains(dayStart),
              let dayEnd = Calendar.current.date(byAdding: .day, value: 1, to: dayStart)
        else { return }

        loadingTranscriptsFor.insert(dayStart)
        transcriptsError = nil
        Task {
            let result = await fetchVoiceMemoTranscripts(
                config: config,
                since: dayStart,
                until: dayEnd,
                search: nil
            )
            await MainActor.run {
                loadingTranscriptsFor.remove(dayStart)
                switch result {
                case .success(let rows):
                    transcriptsByDay[dayStart] = rows
                case .failure(let err):
                    transcriptsError = err.localizedDescription
                }
            }
        }
    }

    private func fetchGlobalTranscripts(force: Bool = false) {
        guard force || globalTranscripts.isEmpty else { return }
        guard !loadingGlobalTranscripts else { return }

        loadingGlobalTranscripts = true
        globalTranscriptsError = nil
        let search = transcriptSearch.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            let result = await fetchVoiceMemoTranscripts(
                config: config,
                search: search.isEmpty ? nil : search
            )
            await MainActor.run {
                loadingGlobalTranscripts = false
                switch result {
                case .success(let rows):
                    globalTranscripts = rows
                case .failure(let err):
                    globalTranscriptsError = err.localizedDescription
                }
            }
        }
    }

    private func loadFrameIfNeeded() {
        guard let frame = currentFrame else {
            currentImage = nil
            currentMeta = nil
            currentLoadedFrameId = nil
            return
        }
        if currentLoadedFrameId == frame.id, currentImage != nil { return }

        if let cached = imageCache[frame.id] {
            currentImage = cached.image
            currentMeta = (cached.app, cached.window)
            currentLoadedFrameId = frame.id
            imageError = nil
            return
        }

        // Show stale image briefly while the new one loads; do NOT clear
        // currentImage so playback doesn't strobe to black.
        let targetId = frame.id
        if inFlightFetches.contains(targetId) { return }
        inFlightFetches.insert(targetId)
        imageError = nil

        Task {
            let result = await fetchScreenshot(config: config, frameId: targetId)
            await MainActor.run {
                inFlightFetches.remove(targetId)
                switch result {
                case .success(let shot):
                    cacheScreenshot(id: targetId, shot: shot)
                    // Only update display if user hasn't scrubbed elsewhere
                    if currentFrame?.id == targetId {
                        currentImage = shot.image
                        currentMeta = (shot.app, shot.window)
                        currentLoadedFrameId = targetId
                        imageError = nil
                    }
                case .failure(let err):
                    if currentFrame?.id == targetId {
                        imageError = err.localizedDescription
                    }
                }
            }
        }
    }

    private func prefetch(around index: Int) {
        guard !frames.isEmpty else { return }
        // During play we lean forward (12 ahead, 2 behind) since the
        // playhead only moves one direction. When paused, balance the
        // radius so left/right scrubs are equally responsive.
        let ahead = playing ? 12 : 4
        let behind = playing ? 2 : 4
        for delta in 1...ahead {
            let i = index + delta
            if frames.indices.contains(i) {
                prefetchFrame(frames[i].id)
            }
        }
        for delta in 1...behind {
            let i = index - delta
            if frames.indices.contains(i) {
                prefetchFrame(frames[i].id)
            }
        }
    }

    private func prefetchFrame(_ id: Int) {
        guard imageCache[id] == nil, !inFlightFetches.contains(id) else { return }
        inFlightFetches.insert(id)
        Task {
            let result = await fetchScreenshot(config: config, frameId: id)
            await MainActor.run {
                inFlightFetches.remove(id)
                if case .success(let shot) = result {
                    cacheScreenshot(id: id, shot: shot)
                }
            }
        }
    }

    private func cacheScreenshot(id: Int, shot: ScreenshotResult) {
        imageCache[id] = shot
        // Simple cap — evict oldest by FIFO when over budget. NSCache
        // would be slightly better but this stays predictable.
        if imageCache.count > 64 {
            let toEvict = imageCache.count - 64
            for key in imageCache.keys.prefix(toEvict) {
                imageCache.removeValue(forKey: key)
            }
        }
    }

    // MARK: playback

    private func jumpBy(_ delta: Int) {
        let count = frames.count
        guard count > 0 else { return }
        let next = max(0, min(count - 1, currentIndex + delta))
        positionByDay[selectedDate] = Double(next)
    }

    private func startPlay() {
        guard !frames.isEmpty else { return }
        playing = true
        let captured = selectedDate
        playTask = Task { @MainActor in
            while playing && !Task.isCancelled {
                let interval = max(0.02, kPlaybackBaseInterval / max(0.01, playSpeed))
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                if !playing || selectedDate != captured { break }
                let count = frames.count
                guard count > 0 else { break }
                if currentIndex >= count - 1 {
                    playing = false
                    break
                }
                // Adaptive frame-skip: if the next frame isn't cached yet,
                // jump straight to the nearest cached frame ahead of the
                // playhead instead of waiting. The slider keeps moving
                // smoothly; we just drop frames the network can't deliver
                // in time. Capped so we never skip more than what the
                // current speed implies (10x = up to 10 frames per tick).
                let maxSkip = max(1, Int(playSpeed.rounded()))
                var nextIndex = currentIndex + 1
                if imageCache[frames[nextIndex].id] == nil {
                    var probe = nextIndex
                    let limit = min(count - 1, nextIndex + maxSkip)
                    while probe < limit, imageCache[frames[probe].id] == nil {
                        probe += 1
                    }
                    // Even if we didn't find a cached one within maxSkip,
                    // advancing maxSkip steps (vs 1) keeps the playhead
                    // chasing the network rather than blocking on it.
                    nextIndex = probe
                }
                positionByDay[selectedDate] = Double(min(count - 1, nextIndex))
            }
        }
    }

    private func stopPlay() {
        playing = false
        playTask?.cancel()
        playTask = nil
    }

    private func fmtClock(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f.string(from: d)
    }

    private func fmtDateTime(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f.string(from: d)
    }

    private func formatAge(_ seconds: Double) -> String {
        let s = max(0, Int(seconds.rounded()))
        if s < 60 { return "\(s)s" }
        let m = s / 60
        if m < 60 { return "\(m)m" }
        return "\(m / 60)h \(m % 60)m"
    }

    private func transcriptCopyBlock(_ row: TranscriptRef) -> String {
        let title = row.title?.isEmpty == false ? " · \(row.title!)" : ""
        let stamp = transcriptScope == .all ? fmtDateTime(row.timestamp) : fmtClock(row.timestamp)
        return "[\(stamp)\(title)]\n\(row.transcript)"
    }

    private func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
