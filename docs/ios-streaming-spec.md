# iOS Broadcast Extension: Real-Time Streaming Spec

## Overview

Modify the `pip` iOS app's BroadcastUploadExtension to stream frames in real-time to the fisherman enclave server, instead of saving to disk and uploading later. The broadcast extension should capture frames, run on-device OCR, and stream metadata + JPEG data over WebSocket to the server.

The server (fisherman enclave) handles all encryption and storage. The client sends plaintext over TLS.

## Current Architecture (pip)

```
ReplayKit capture → save JPEG to disk → user manually uploads → chat with OpenAI
```

## Target Architecture

```
ReplayKit capture → Vision OCR (on-device) → WebSocket stream to enclave server
```

## What Changes

### BroadcastUploadExtension (`SampleHandler.swift`)

The extension currently captures frames every 200ms, converts to JPEG, and writes to the App Group container. Change it to:

1. **Keep** the frame rate throttling (configurable interval via `SharedConfig`)
2. **Keep** saving `latest_frame.jpg` for the main app's PiP preview
3. **Remove** saving sequential `frame_XXXX.jpg` to session directories (server stores them now)
4. **Add** Vision OCR on each captured frame
5. **Add** WebSocket connection to the enclave server
6. **Add** dHash-based dedup to skip unchanged frames
7. **Stream** each frame as JSON + base64 JPEG over WebSocket

### Frame Processing Pipeline

For each captured frame:

```
CVPixelBuffer
  → CGImage → UIImage
  → save to latest_frame.jpg (for PiP preview, keep as-is)
  → run VNRecognizeTextRequest (Vision OCR)
  → compute dHash (perceptual hash for dedup)
  → if dHash distance from previous frame < threshold (6): skip
  → encode JPEG (quality 0.6, max dimension 960px)
  → build JSON payload
  → send over WebSocket
```

### Vision OCR

Use Apple Vision framework in the broadcast extension:

```swift
import Vision

func performOCR(on cgImage: CGImage, completion: @escaping (String, [String]) -> Void) {
    let request = VNRecognizeTextRequest { request, error in
        guard let observations = request.results as? [VNRecognizedTextObservation] else {
            completion("", [])
            return
        }

        let text = observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")

        // Extract URLs from recognized text
        let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)
        let urls = detector?.matches(in: text, range: NSRange(text.startIndex..., in: text))
            .compactMap { $0.url?.absoluteString } ?? []

        completion(text, urls)
    }
    request.recognitionLevel = .fast  // Use .fast in extension for performance
    request.usesLanguageCorrection = false  // Faster without

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try? handler.perform([request])
}
```

**Note:** First Vision OCR call takes ~1.4s (framework JIT warmup). Subsequent calls ~40ms at 960px. Consider running a dummy OCR call on `broadcastStarted` to warm up.

### dHash Dedup

Compute a 64-bit perceptual hash to skip visually identical frames:

```swift
func dhash(image: CGImage, hashSize: Int = 9) -> UInt64 {
    // 1. Resize to (hashSize x (hashSize-1)) grayscale
    // 2. Compare adjacent pixels left-to-right
    // 3. Pack into 64-bit integer
    // Return hamming distance between current and previous hash
}
```

Skip frame if hamming distance from previous hash is < 6 (configurable via SharedConfig). This prevents sending duplicate frames when the screen hasn't changed.

### WebSocket Connection

Open a persistent WebSocket to the enclave server. The extension process can maintain a URLSessionWebSocketTask:

```swift
class FrameStreamer {
    private var webSocket: URLSessionWebSocketTask?
    private let serverURL: URL
    private let authToken: String

    func connect() {
        var request = URLRequest(url: serverURL)
        if !authToken.isEmpty {
            request.setValue("Bearer \(authToken)", forHTTPHeaderField: "Authorization")
        }
        webSocket = URLSession.shared.webSocketTask(with: request)
        webSocket?.resume()
        listenForMessages()
    }

    func send(payload: Data) {
        webSocket?.send(.data(payload)) { error in
            if let error = error {
                // Log, attempt reconnect
            }
        }
    }

    // Auto-reconnect with exponential backoff (1s, 2s, 4s, ... max 30s)
}
```

**Config** (read from `SharedConfig` / App Group UserDefaults):
- `server_url`: WebSocket URL, e.g. `wss://enclave.example.com:9999/ingest`
- `auth_token`: Bearer token for authentication
- Set from the main app's settings UI, stored in shared UserDefaults

### Wire Format

Each frame sent as a JSON text message over WebSocket. Must match this exact schema (the enclave server parses this format):

```json
{
  "type": "frame",
  "ts": 1709840000.123,
  "app": null,
  "bundle": null,
  "window": null,
  "ocr_text": "Hello world\nSecond line of text...",
  "urls": ["https://example.com"],
  "image": "<base64-encoded-JPEG>",
  "w": 960,
  "h": 540,
  "tier_hint": 1,
  "routing_signals": {
    "dhash_distance": 4,
    "ocr_text_length": 312,
    "ocr_url_count": 1,
    "bundle_id": "",
    "is_text_heavy_app": false
  }
}
```

Field details:

| Field | Type | Description |
|---|---|---|
| `type` | string | Always `"frame"` |
| `ts` | float | Unix timestamp with millisecond precision (`Date().timeIntervalSince1970`) |
| `app` | string? | Always `null` on iOS (no foreground app detection) |
| `bundle` | string? | Always `null` on iOS |
| `window` | string? | Always `null` on iOS |
| `ocr_text` | string | Full Vision OCR output, newline-separated |
| `urls` | [string] | URLs extracted from OCR text |
| `image` | string | Base64-encoded JPEG data |
| `w` | int | Image width in pixels |
| `h` | int | Image height in pixels |
| `tier_hint` | int | `1` if OCR text is long enough (>50 chars), else `2` |
| `routing_signals` | object | Routing metadata for the server |

### Tier Routing (simplified for iOS)

Since iOS can't detect the foreground app, routing is simpler:

```swift
func computeTier(ocrText: String, dhashDistance: Int) -> Int {
    let hasEnoughText = ocrText.count >= 50
    let lowVisualChange = dhashDistance < 20
    if hasEnoughText && lowVisualChange { return 1 }
    return 2
}
```

### Image Sizing

Before encoding to JPEG, resize the captured frame so the longest edge is at most 960px. The broadcast extension captures at full screen resolution (e.g. 2556x1179 on iPhone 15 Pro), which is unnecessarily large.

```swift
func resizeImage(_ image: UIImage, maxDimension: CGFloat = 960) -> UIImage {
    let size = image.size
    let scale = min(maxDimension / max(size.width, size.height), 1.0)
    if scale >= 1.0 { return image }
    let newSize = CGSize(width: size.width * scale, height: size.height * scale)
    UIGraphicsBeginImageContextWithOptions(newSize, true, 1.0)
    image.draw(in: CGRect(origin: .zero, size: newSize))
    let resized = UIGraphicsGetImageFromCurrentImageContext()!
    UIGraphicsEndImageContext()
    return resized
}
```

JPEG quality: 0.6 (matches macOS daemon).

### Memory Budget

Broadcast extensions have a ~50MB memory limit. Budget:

| Component | Estimated Memory |
|---|---|
| Vision framework (after warmup) | ~15MB |
| WebSocket connection | ~1MB |
| Frame buffer (1 frame at 960px) | ~2MB |
| OCR result strings | <1MB |
| dHash computation | <1MB |
| **Total** | **~20MB** |

This leaves comfortable headroom. Key rules:
- Process one frame at a time, don't queue frames in memory
- Release CGImage/UIImage references immediately after encoding
- If WebSocket send fails, drop the frame (don't buffer)

### Error Handling

- **WebSocket disconnected**: reconnect with exponential backoff (1s → 2s → 4s → ... → 30s max). Drop frames while disconnected.
- **OCR fails**: send the frame with `ocr_text: ""` and `urls: []`
- **Memory pressure**: if the OS sends a memory warning, skip OCR and send frames with empty text until pressure subsides
- **Extension killed by OS**: this is normal. The user restarts the broadcast from Control Center. No cleanup needed.

### SharedConfig Additions

Add to `SharedConfig.swift` (shared UserDefaults keys):

```swift
// Streaming server
static let serverURLKey = "server_url"
static let authTokenKey = "auth_token"

// Defaults
static let serverURLDefault = "wss://localhost:9999/ingest"

static var serverURL: String {
    sharedDefaults?.string(forKey: serverURLKey) ?? serverURLDefault
}

static var authToken: String {
    sharedDefaults?.string(forKey: authTokenKey) ?? ""
}
```

### Main App Changes

1. **Settings screen**: Add fields for server URL and auth token (stored in shared UserDefaults)
2. **Remove batch upload flow**: The `SessionFramesViewController` upload pipeline is no longer needed for real-time streaming. Keep it as an option for manual/offline upload if desired.
3. **Session list**: Sessions are now server-side. The main app could query the server for session history instead of reading local directories. (Optional — can defer this.)

### What NOT to Change

- **PiP preview**: Keep saving `latest_frame.jpg` and the Darwin notification for the main app's real-time preview
- **Main app UI**: Keep the existing session browsing / chat UI for now
- **Capture interval**: Keep the existing configurable interval (default 200ms, but recommend 1000ms for streaming to match macOS daemon)

## Testing

1. Build and run the broadcast extension
2. Start screen recording from Control Center
3. Run the fisherman server locally: `cd server && uv run python ingest.py` (needs Postgres + R2 configured, or mock them)
4. Alternatively, test with `websocat ws://localhost:9999/ingest` to see raw JSON payloads
5. Verify: frames arrive with `ocr_text` populated, `image` is valid base64 JPEG, duplicate frames are skipped

## Dependencies

No new third-party dependencies needed. Everything uses iOS system frameworks:
- `ReplayKit` (already imported)
- `Vision` (for OCR)
- `VideoToolbox` (already imported)
- `Foundation` URLSessionWebSocketTask (for WebSocket)
