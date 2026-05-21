import AppKit
import Foundation
import SwiftUI

private let APPROACH_HALO_PX: CGFloat = 160
private let SURFACE_SIDE_KEY = "harness_presence_side"
private let SURFACE_TOP_KEY = "harness_presence_top_inset"
private let DEFAULT_TOP_INSET: CGFloat = 86
private let EDGE_INSET: CGFloat = 18

private enum SurfaceSide: String {
    case left
    case right
}

final class HarnessFloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class NotchCoordinator {
    private let client: HarnessClient
    let state = HarnessState()

    private var panel: NSPanel?
    private var hostingView: NSHostingView<HarnessFloatingSurface>?
    private var pollTimer: Timer?
    private var visibilityTimer: Timer?
    private var spaceObserver: Any?
    private var screenObserver: Any?
    private var collapseTask: Task<Void, Never>?

    private var surfaceSide: SurfaceSide
    private var surfaceTopInset: CGFloat

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
        self.surfaceSide = SurfaceSide(rawValue: UserDefaults.standard.string(forKey: SURFACE_SIDE_KEY) ?? "") ?? .right
        let savedTop = UserDefaults.standard.object(forKey: SURFACE_TOP_KEY) == nil
            ? DEFAULT_TOP_INSET
            : CGFloat(UserDefaults.standard.double(forKey: SURFACE_TOP_KEY))
        self.surfaceTopInset = max(EDGE_INSET, savedTop)

        state.actionHandler = { [weak self] action in
            Task { @MainActor in self?.onAction(action: action) }
        }
        state.hoverHandler = { [weak self] target, entered in
            self?.recordEvent(kind: entered ? "hover_start" : "hover_end", target: target)
        }
        state.surfaceHoverHandler = { [weak self] hovering in
            self?.handleSurfaceHover(hovering)
        }
        state.togglePinHandler = { [weak self] in
            self?.togglePinned()
        }
        state.dragHandler = { [weak self] delta in
            self?.dragSurface(delta: delta)
        }
        state.dragEndHandler = { [weak self] in
            self?.snapAndPersistSurface()
        }
    }

    func start() {
        showPanel()
        startSurfaceVisibilityWatchdog()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.poll() }
        }
    }

    func stop() {
        pollTimer?.invalidate()
        stopSurfaceVisibilityWatchdog()
        collapseTask?.cancel()
        autoDismissTask?.cancel()
        stopMouseTracking()
        panel?.close()
        panel = nil
    }

    // MARK: - Surface

    private func showPanel() {
        let view = HarnessFloatingSurface(state: state)
        let host = NSHostingView(rootView: view)
        let initialFrame = frameForCurrentState()
        host.frame = NSRect(origin: .zero, size: initialFrame.size)
        hostingView = host

        let panel = HarnessFloatingPanel(
            contentRect: initialFrame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        panel.contentView = host
        panel.hasShadow = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        self.panel = panel
        configurePersistentSurface(panel)
        ensureSurfaceVisible(repositionIfNeeded: true)
    }

    private func configurePersistentSurface(_ panel: NSPanel) {
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.level = .floating
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .stationary,
            .ignoresCycle,
        ]
    }

    private func startSurfaceVisibilityWatchdog() {
        stopSurfaceVisibilityWatchdog()
        visibilityTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.ensureSurfaceVisible(repositionIfNeeded: false) }
        }
        spaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.ensureSurfaceVisible(repositionIfNeeded: false) }
        }
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.ensureSurfaceVisible(repositionIfNeeded: true) }
        }
    }

    private func stopSurfaceVisibilityWatchdog() {
        visibilityTimer?.invalidate()
        visibilityTimer = nil
        if let spaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(spaceObserver)
            self.spaceObserver = nil
        }
        if let screenObserver {
            NotificationCenter.default.removeObserver(screenObserver)
            self.screenObserver = nil
        }
    }

    private func ensureSurfaceVisible(repositionIfNeeded: Bool) {
        guard let panel else {
            showPanel()
            return
        }
        let size = currentSurfaceSize()
        hostingView?.frame = NSRect(origin: .zero, size: size)
        configurePersistentSurface(panel)

        let screen = screenForPanel() ?? primaryScreen()
        let frame = panel.frame
        let sizeChanged = abs(frame.width - size.width) > 1 || abs(frame.height - size.height) > 1
        if repositionIfNeeded || sizeChanged || !hasUsableVisibleArea(frame, on: screen) {
            let next = hasUsableVisibleArea(frame, on: screen)
                ? clampedFrame(NSRect(origin: frame.origin, size: size), screen: screen)
                : frameForCurrentState()
            panel.setFrame(next, display: true, animate: false)
        }
        panel.setIsVisible(true)
        panel.orderFrontRegardless()
    }

    private func hasUsableVisibleArea(_ frame: NSRect, on screen: NSScreen?) -> Bool {
        let visible = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? frame
        let intersection = frame.intersection(visible)
        return intersection.width >= min(64, frame.width * 0.5)
            && intersection.height >= min(24, frame.height * 0.5)
    }

    private func setSurfaceExpanded(_ expanded: Bool) {
        collapseTask?.cancel()
        if !expanded, state.surfacePinned || currentDecisionID != nil {
            return
        }
        withAnimation(.smooth(duration: 0.16)) {
            state.surfaceExpanded = expanded
        }
        resizeSurface()
        if !expanded {
            state.activePanel = currentDecisionID == nil ? .pipeline : .ping
        }
    }

    private func handleSurfaceHover(_ hovering: Bool) {
        collapseTask?.cancel()
        if hovering {
            setSurfaceExpanded(true)
            return
        }

        guard !state.surfacePinned, currentDecisionID == nil else { return }
        collapseTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)
            await MainActor.run {
                guard let self else { return }
                if self.mouseIsInsideSurface() { return }
                self.setSurfaceExpanded(false)
            }
        }
    }

    private func togglePinned() {
        state.surfacePinned.toggle()
        setSurfaceExpanded(state.surfacePinned)
    }

    private func resizeSurface() {
        guard let panel else { return }
        let old = panel.frame
        let size = currentSurfaceSize()
        hostingView?.frame = NSRect(origin: .zero, size: size)
        let screen = screenForPanel() ?? primaryScreen()
        let x: CGFloat
        if surfaceSide == .right {
            x = old.maxX - size.width
        } else {
            x = old.minX
        }
        let y = old.maxY - size.height
        let next = clampedFrame(NSRect(origin: NSPoint(x: x, y: y), size: size), screen: screen)
        panel.setFrame(next, display: true, animate: true)
    }

    private func currentSurfaceSize() -> CGSize {
        if !state.surfaceExpanded {
            return CGSize(width: 132, height: 38)
        }
        switch state.activePanel {
        case .ping:
            return CGSize(width: 620, height: 176)
        case .pipeline:
            return CGSize(width: 780, height: 384)
        case .diet:
            return CGSize(width: 780, height: 418)
        case .settings:
            return CGSize(width: 780, height: 618)
        }
    }

    private func frameForCurrentState() -> NSRect {
        let size = currentSurfaceSize()
        let screen = primaryScreen()
        let frame = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        let top = min(surfaceTopInset, max(EDGE_INSET, frame.height - size.height - EDGE_INSET))
        let x = surfaceSide == .right
            ? frame.maxX - size.width - EDGE_INSET
            : frame.minX + EDGE_INSET
        let y = frame.maxY - top - size.height
        return clampedFrame(NSRect(x: x, y: y, width: size.width, height: size.height), screen: screen)
    }

    private func dragSurface(delta: CGSize) {
        guard let panel else { return }
        var frame = panel.frame
        frame.origin.x += delta.width
        frame.origin.y -= delta.height
        panel.setFrame(clampedFrame(frame, screen: screenForPanel()), display: true)
    }

    private func snapAndPersistSurface() {
        guard let panel else { return }
        let screen = screenForPanel() ?? primaryScreen()
        let visible = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? panel.frame
        var frame = panel.frame
        surfaceSide = frame.midX < visible.midX ? .left : .right
        frame.origin.x = surfaceSide == .right
            ? visible.maxX - frame.width - EDGE_INSET
            : visible.minX + EDGE_INSET
        frame = clampedFrame(frame, screen: screen)
        surfaceTopInset = max(EDGE_INSET, visible.maxY - frame.maxY)
        panel.setFrame(frame, display: true, animate: true)
        UserDefaults.standard.set(surfaceSide.rawValue, forKey: SURFACE_SIDE_KEY)
        UserDefaults.standard.set(Double(surfaceTopInset), forKey: SURFACE_TOP_KEY)
    }

    private func clampedFrame(_ frame: NSRect, screen: NSScreen?) -> NSRect {
        let visible = screen?.visibleFrame ?? NSScreen.main?.visibleFrame ?? frame
        var next = frame
        next.origin.x = min(max(next.origin.x, visible.minX + EDGE_INSET), visible.maxX - next.width - EDGE_INSET)
        next.origin.y = min(max(next.origin.y, visible.minY + EDGE_INSET), visible.maxY - next.height - EDGE_INSET)
        return next
    }

    private func mouseIsInsideSurface() -> Bool {
        guard let panel else { return false }
        return panel.frame.insetBy(dx: -10, dy: -10).contains(NSEvent.mouseLocation)
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
        setSurfaceExpanded(true)
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
        if !state.surfacePinned {
            collapseTask?.cancel()
            collapseTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 650_000_000)
                await MainActor.run { self?.setSurfaceExpanded(false) }
            }
        }
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
        let handler: (NSEvent) -> Void = { [weak self] _ in
            guard let self = self else { return }
            let near = self.computeApproachRect().contains(NSEvent.mouseLocation)
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
        state.surfaceExpanded || lastWasNearPill || !activeHoverTargets.isEmpty
    }

    private func computeApproachRect() -> NSRect {
        guard let panel else { return .zero }
        return panel.frame.insetBy(dx: -APPROACH_HALO_PX, dy: -APPROACH_HALO_PX)
    }

    private func screenForPanel() -> NSScreen? {
        guard let panel else { return primaryScreen() }
        return NSScreen.screens.first { $0.frame.intersects(panel.frame) } ?? primaryScreen()
    }

    private func primaryScreen() -> NSScreen? {
        if let notched = NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) {
            return notched
        }
        return NSScreen.main ?? NSScreen.screens.first
    }
}
