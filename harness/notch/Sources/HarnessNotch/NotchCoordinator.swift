import AppKit
import Combine
import DynamicNotchKit
import Foundation
import SwiftUI

private let APPROACH_HALO_PX: CGFloat = 200
private let HARNESS_NOTCH_OFFSET_X: CGFloat = 390

@MainActor
final class NotchCoordinator {
    private let client: HarnessClient
    let state = HarnessState()

    // The DynamicNotch instance. Generic over the three view types so we don't
    // wrap everything in AnyView.
    private var notch: DynamicNotch<HarnessExpanded, HarnessCompactLeading, HarnessCompactTrailing>?
    private var hoverCancellable: AnyCancellable?
    private var pollTimer: Timer?

    private var currentDecisionID: String?
    private var displayedAt: Date?
    private var autoDismissTask: Task<Void, Never>?

    // Interaction tracking
    private var events: [InteractionEvent] = []
    private var mouseMonitorGlobal: Any?
    private var mouseMonitorLocal: Any?
    private var lastWasNearPill = false
    private var activeHoverTargets = Set<String>()

    init(baseURL: String) {
        self.client = HarnessClient(baseURLString: baseURL)

        // Install handlers up front so views can call into the coordinator.
        state.actionHandler = { [weak self] action in
            Task { @MainActor in self?.onAction(action: action) }
        }
        state.hoverHandler = { [weak self] target, entered in
            self?.recordEvent(kind: entered ? "hover_start" : "hover_end", target: target)
        }
    }

    func start() {
        let st = state
        notch = DynamicNotch(
            hoverBehavior: .all,
            style: .auto,
            horizontalOffset: HARNESS_NOTCH_OFFSET_X
        ) {
            HarnessExpanded(state: st)
        } compactLeading: {
            HarnessCompactLeading(state: st)
        } compactTrailing: {
            HarnessCompactTrailing(state: st)
        }

        let n = notch!
        hoverCancellable = n.$isHovering
            .debounce(for: .milliseconds(120), scheduler: DispatchQueue.main)
            .removeDuplicates()
            .sink { hovering in
                Task { @MainActor in
                    if hovering {
                        await n.expand()
                    } else {
                        await n.compact()
                    }
                }
            }

        Task { await n.compact() }

        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    func stop() {
        pollTimer?.invalidate()
        hoverCancellable?.cancel()
        autoDismissTask?.cancel()
        stopMouseTracking()
        state.activePanel = .pipeline
        let n = notch
        Task { await n?.hide() }
    }

    // MARK: - Polling

    private func poll() {
        if currentDecisionID != nil { return }
        client.getPending { [weak self] pending in
            guard let self = self, let pending = pending else { return }
            Task { @MainActor in self.show(pending: pending) }
        }
    }

    // MARK: - Show / dismiss

    private func show(pending: PendingPayload) {
        currentDecisionID = pending.decisionID
        displayedAt = Date()
        events.removeAll(keepingCapacity: true)
        activeHoverTargets.removeAll(keepingCapacity: true)
        lastWasNearPill = false
        state.activePanel = .ping
        state.current = pending

        let n = notch
        Task { await n?.compact() }
        startMouseTracking()

        autoDismissTask?.cancel()
        autoDismissTask = Task { [weak self, decisionID = pending.decisionID] in
            let fallbackDelay = 8.0
            let initialDelay = pending.expiresAtUnix.map {
                max(1.0, min(60.0, $0 - Date().timeIntervalSince1970))
            } ?? fallbackDelay
            try? await Task.sleep(nanoseconds: UInt64(initialDelay * 1_000_000_000))
            if Task.isCancelled { return }
            var extraWait = 0.0
            while self?.isInspectingPill() == true && extraWait < 30.0 {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                if Task.isCancelled { return }
                extraWait += 1.0
            }
            await MainActor.run {
                guard let self = self, self.currentDecisionID == decisionID else { return }
                self.onAction(action: "timed_out")
            }
        }
    }

    private func onAction(action: String) {
        guard let did = currentDecisionID else { return }
        currentDecisionID = nil
        autoDismissTask?.cancel()
        autoDismissTask = nil
        stopMouseTracking()

        let latency = displayedAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? 0
        closeOpenHovers(atMs: latency)
        displayedAt = nil

        let collected = events
        events.removeAll(keepingCapacity: true)
        activeHoverTargets.removeAll(keepingCapacity: true)
        client.postOutcome(
            decisionID: did,
            action: action,
            latencyMs: latency,
            interactions: collected
        )

        state.current = nil
        state.activePanel = .pipeline
        let n = notch
        Task { await n?.compact() }
    }

    // MARK: - Interaction tracking

    private func recordEvent(kind: String, target: String? = nil) {
        guard let start = displayedAt else { return }
        let t_ms = Int(Date().timeIntervalSince(start) * 1000)
        recordEvent(kind: kind, target: target, tMs: t_ms)
    }

    private func recordEvent(kind: String, target: String? = nil, tMs: Int) {
        if let target = target {
            if kind == "hover_start" {
                activeHoverTargets.insert(target)
            } else if kind == "hover_end" {
                activeHoverTargets.remove(target)
            }
        }
        events.append(InteractionEvent(t_ms: tMs, kind: kind, target: target))
    }

    private func closeOpenHovers(atMs tMs: Int) {
        for target in activeHoverTargets.sorted() {
            recordEvent(kind: "hover_end", target: target, tMs: tMs)
        }
    }

    private func startMouseTracking() {
        stopMouseTracking()
        let halo = computeApproachRect()
        let handler: (NSEvent) -> Void = { [weak self] _ in
            guard let self = self else { return }
            let p = NSEvent.mouseLocation
            let near = halo.contains(p)
            if near != self.lastWasNearPill {
                self.recordEvent(kind: near ? "approach" : "leave_proximity")
                self.lastWasNearPill = near
            }
        }
        mouseMonitorGlobal = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved], handler: handler)
        mouseMonitorLocal = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved]) { ev in
            handler(ev); return ev
        }
    }

    private func stopMouseTracking() {
        if let m = mouseMonitorGlobal { NSEvent.removeMonitor(m); mouseMonitorGlobal = nil }
        if let m = mouseMonitorLocal  { NSEvent.removeMonitor(m); mouseMonitorLocal  = nil }
    }

    private func isInspectingPill() -> Bool {
        lastWasNearPill || !activeHoverTargets.isEmpty
    }

    /// Compute a rough approach rectangle around the visible pill. We don't
    /// have direct access to DynamicNotch's window frame, so approximate from
    /// screen geometry: top-center of the primary (preferably notched) screen.
    private func computeApproachRect() -> NSRect {
        guard let screen = primaryScreen() else { return .zero }
        let frame = screen.frame
        let pillW: CGFloat = 600
        let pillH: CGFloat = 120
        let cx = frame.midX
        let topY = frame.maxY
        let pillRect = NSRect(
            x: cx - pillW / 2 + HARNESS_NOTCH_OFFSET_X,
            y: topY - pillH,
            width: pillW,
            height: pillH
        )
        return pillRect.insetBy(dx: -APPROACH_HALO_PX, dy: -APPROACH_HALO_PX)
    }

    private func primaryScreen() -> NSScreen? {
        if let notched = NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) {
            return notched
        }
        return NSScreen.main ?? NSScreen.screens.first
    }
}
