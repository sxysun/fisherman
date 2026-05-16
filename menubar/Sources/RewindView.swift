import SwiftUI
import AppKit
import Foundation

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
            let (data, response) = try await URLSession.shared.data(for: request)
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

@MainActor
func fetchScreenshot(
    config: ConfigManager,
    frameId: Int
) async -> Result<ScreenshotResult, RewindFetchError> {
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
            let (data, response) = try await URLSession.shared.data(for: request)
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

    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
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
        VStack(alignment: .leading, spacing: 10) {
            navHeader
            Divider().opacity(0.4)
            framePreview
            scrubber
            controls
        }
        .onAppear {
            fetchIndexIfNeeded(selectedDate)
            loadFrameIfNeeded()
        }
        .onChange(of: selectedDate) { _, _ in
            stopPlay()
            fetchIndexIfNeeded(selectedDate)
            currentImage = nil
            currentMeta = nil
            currentLoadedFrameId = nil
            loadFrameIfNeeded()
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

            Text(dateLabel)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.primary)
                .frame(minWidth: 200, alignment: .leading)

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
                fetchIndexIfNeeded(selectedDate)
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered).controlSize(.small)
            .help("Refresh this day's index")
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
        ZStack(alignment: .bottomLeading) {
            Color.black

            if let img = currentImage {
                Image(nsImage: img)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let err = imageError {
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
            } else if currentFrame != nil {
                ProgressView().controlSize(.small)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            // Overlay: timestamp + app + window
            if let f = currentFrame {
                HStack(spacing: 6) {
                    Text(fmtClock(f.timestamp))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.white)
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
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(Color.black.opacity(0.55))
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
        return "\(frames.count) frames · \(fmtClock(frames.first!.timestamp)) → \(fmtClock(frames.last!.timestamp))"
    }

    // MARK: fetching

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
        let radius = playing ? 4 : 2
        for delta in 1...radius {
            let ahead = index + delta
            if frames.indices.contains(ahead) {
                prefetchFrame(frames[ahead].id)
            }
            let behind = index - delta
            if frames.indices.contains(behind) {
                prefetchFrame(frames[behind].id)
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
                positionByDay[selectedDate] = Double(currentIndex + 1)
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
}
