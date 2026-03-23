import CoreGraphics
import CoreImage
import CoreMedia
import Foundation
import ScreenCaptureKit

struct LatestDisplayFrame {
    let image: CGImage
    let width: Int
    let height: Int
    let timestamp: TimeInterval
    let displayID: CGDirectDisplayID
}

private final class DisplayStreamCapture: NSObject, SCStreamDelegate, SCStreamOutput {
    private let display: SCDisplay
    private let maxDimension: Int
    private let outputQueue: DispatchQueue
    private let ciContext = CIContext()
    private let lock = NSLock()
    private let frameSignal = DispatchSemaphore(value: 0)

    private var latestFrame: LatestDisplayFrame?
    private var stream: SCStream?
    private var frameInterval: TimeInterval

    init(display: SCDisplay, frameInterval: TimeInterval, maxDimension: Int) {
        self.display = display
        self.frameInterval = frameInterval
        self.maxDimension = maxDimension
        self.outputQueue = DispatchQueue(
            label: "fish.stream.display.\(display.displayID)",
            qos: .userInitiated
        )
        super.init()
    }

    var displayID: CGDirectDisplayID {
        CGDirectDisplayID(display.displayID)
    }

    var bounds: CGRect {
        display.frame
    }

    func start() async throws {
        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )
        let config = makeConfiguration(frameInterval: frameInterval)
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: outputQueue)
        self.stream = stream
        try await stream.startCapture()
    }

    func stop() {
        guard let stream else { return }
        self.stream = nil
        Task {
            try? await stream.stopCapture()
        }
    }

    func updateFrameInterval(_ seconds: TimeInterval) {
        guard abs(seconds - frameInterval) > 0.01 else { return }
        frameInterval = seconds
        guard let stream else { return }
        let config = makeConfiguration(frameInterval: seconds)
        Task {
            try? await stream.updateConfiguration(config)
        }
    }

    func latestFrame(waitUpTo timeout: TimeInterval, maxAge: TimeInterval) -> LatestDisplayFrame? {
        if let frame = currentFrame(maxAge: maxAge) {
            return frame
        }
        guard timeout > 0 else {
            return currentFrame(maxAge: maxAge)
        }
        _ = frameSignal.wait(timeout: .now() + timeout)
        return currentFrame(maxAge: maxAge)
    }

    private func currentFrame(maxAge: TimeInterval) -> LatestDisplayFrame? {
        lock.lock()
        defer { lock.unlock() }
        guard let latestFrame else { return nil }
        guard (ProcessInfo.processInfo.systemUptime - latestFrame.timestamp) <= maxAge else {
            return nil
        }
        return latestFrame
    }

    private func makeConfiguration(frameInterval: TimeInterval) -> SCStreamConfiguration {
        let config = SCStreamConfiguration()
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.showsCursor = true
        config.queueDepth = 3
        config.minimumFrameInterval = CMTime(seconds: frameInterval, preferredTimescale: 600)

        let nativeWidth = max(Int(display.frame.width), 1)
        let nativeHeight = max(Int(display.frame.height), 1)
        let scale = min(Double(maxDimension) / Double(max(nativeWidth, nativeHeight)), 1.0)
        config.width = max(Int(Double(nativeWidth) * scale), 1)
        config.height = max(Int(Double(nativeHeight) * scale), 1)
        return config
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen, sampleBuffer.isValid else { return }
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let ciImage = CIImage(cvPixelBuffer: imageBuffer)
        let frameRect = CGRect(
            x: 0,
            y: 0,
            width: CVPixelBufferGetWidth(imageBuffer),
            height: CVPixelBufferGetHeight(imageBuffer)
        )
        guard let cgImage = ciContext.createCGImage(ciImage, from: frameRect) else { return }

        let frame = LatestDisplayFrame(
            image: cgImage,
            width: cgImage.width,
            height: cgImage.height,
            timestamp: ProcessInfo.processInfo.systemUptime,
            displayID: displayID
        )

        lock.lock()
        latestFrame = frame
        lock.unlock()
        frameSignal.signal()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock()
        latestFrame = nil
        lock.unlock()
        NSLog("Fisherman ScreenCaptureKit stream stopped: %@", error.localizedDescription)
    }
}

final class ScreenStreamManager {
    private let maxDimension: Int
    private let lock = NSLock()
    private var captures: [CGDirectDisplayID: DisplayStreamCapture] = [:]
    private var frameInterval: TimeInterval
    private var started = false

    init(frameInterval: TimeInterval, maxDimension: Int) {
        self.frameInterval = frameInterval
        self.maxDimension = maxDimension
    }

    func start() {
        lock.lock()
        guard !started else {
            lock.unlock()
            return
        }
        started = true
        let interval = frameInterval
        lock.unlock()

        Task {
            do {
                let shareableContent = try await SCShareableContent.excludingDesktopWindows(
                    false,
                    onScreenWindowsOnly: true
                )
                var startedCaptures: [CGDirectDisplayID: DisplayStreamCapture] = [:]
                for display in shareableContent.displays {
                    let capture = DisplayStreamCapture(
                        display: display,
                        frameInterval: interval,
                        maxDimension: maxDimension
                    )
                    try await capture.start()
                    startedCaptures[capture.displayID] = capture
                }
                lock.lock()
                captures = startedCaptures
                lock.unlock()
            } catch {
                lock.lock()
                started = false
                captures = [:]
                lock.unlock()
                NSLog("Fisherman failed to start ScreenCaptureKit streams: %@", error.localizedDescription)
            }
        }
    }

    func stop() {
        let activeCaptures: [DisplayStreamCapture]
        lock.lock()
        activeCaptures = Array(captures.values)
        captures = [:]
        started = false
        lock.unlock()

        for capture in activeCaptures {
            capture.stop()
        }
    }

    func updateFrameInterval(_ seconds: TimeInterval) {
        let activeCaptures: [DisplayStreamCapture]
        lock.lock()
        frameInterval = seconds
        activeCaptures = Array(captures.values)
        lock.unlock()

        for capture in activeCaptures {
            capture.updateFrameInterval(seconds)
        }
    }

    func latestFrame(
        preferredDisplayID: CGDirectDisplayID?,
        waitUpTo timeout: TimeInterval
    ) -> LatestDisplayFrame? {
        let currentCaptures = snapshotCaptures()
        let maxAge = max(frameInterval * 2.5, 1.0)

        if let preferredDisplayID, let capture = currentCaptures[preferredDisplayID],
           let frame = capture.latestFrame(waitUpTo: timeout, maxAge: maxAge)
        {
            return frame
        }

        let sortedCaptures = currentCaptures.values.sorted { $0.displayID < $1.displayID }
        for capture in sortedCaptures {
            if let frame = capture.latestFrame(waitUpTo: 0, maxAge: maxAge) {
                return frame
            }
        }
        return nil
    }

    func displayID(for rect: CGRect) -> CGDirectDisplayID? {
        let currentCaptures = snapshotCaptures()
        var bestDisplayID: CGDirectDisplayID?
        var bestArea: CGFloat = 0

        for (displayID, capture) in currentCaptures {
            let intersection = rect.intersection(capture.bounds)
            guard !intersection.isNull, !intersection.isEmpty else { continue }
            let area = intersection.width * intersection.height
            if area > bestArea {
                bestArea = area
                bestDisplayID = displayID
            }
        }

        return bestDisplayID
    }

    private func snapshotCaptures() -> [CGDirectDisplayID: DisplayStreamCapture] {
        lock.lock()
        defer { lock.unlock() }
        return captures
    }
}
