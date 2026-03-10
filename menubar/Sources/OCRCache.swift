import Foundation

/// Caches OCR results keyed by image dhash to avoid redundant Vision OCR calls.
/// Thread-safe via NSLock. Uses TTL-based expiry and LRU eviction.
class OCRCache {

    private struct Entry {
        let result: OCRResult
        let timestamp: TimeInterval
    }

    private var cache: [UInt64: Entry] = [:]
    private let ttl: TimeInterval
    private let maxEntries: Int
    private let lock = NSLock()

    init(ttl: TimeInterval = 300, maxEntries: Int = 100) {
        self.ttl = ttl
        self.maxEntries = maxEntries
    }

    /// Look up cached OCR result by image dhash. Returns nil on miss or expiry.
    func get(_ hash: UInt64) -> OCRResult? {
        lock.lock()
        defer { lock.unlock() }

        guard let entry = cache[hash] else { return nil }
        let now = Date().timeIntervalSince1970
        if now - entry.timestamp > ttl {
            cache.removeValue(forKey: hash)
            return nil
        }
        return entry.result
    }

    /// Store an OCR result keyed by image dhash.
    func set(_ hash: UInt64, result: OCRResult) {
        lock.lock()
        defer { lock.unlock() }

        // Evict oldest entry if at capacity
        if cache.count >= maxEntries {
            if let oldest = cache.min(by: { $0.value.timestamp < $1.value.timestamp }) {
                cache.removeValue(forKey: oldest.key)
            }
        }

        cache[hash] = Entry(result: result, timestamp: Date().timeIntervalSince1970)
    }

    func clear() {
        lock.lock()
        defer { lock.unlock() }
        cache.removeAll()
    }
}
