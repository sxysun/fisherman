import Darwin
import Foundation

final class FrameSocketClient {
    private let socketPath: String
    private let magic = Data("FISHBIN1".utf8)

    init(path: String) {
        self.socketPath = (path as NSString).expandingTildeInPath
    }

    @discardableResult
    func sendFrame(metadata: Data, jpeg: Data) -> Bool {
        guard !socketPath.isEmpty else { return false }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)

        let maxPathLength = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count < maxPathLength else { return false }

        withUnsafeMutableBytes(of: &addr.sun_path) { rawBuffer in
            rawBuffer.initializeMemory(as: UInt8.self, repeating: 0, count: rawBuffer.count)
            for (index, byte) in pathBytes.enumerated() {
                rawBuffer[index] = byte
            }
        }

        let addrLength = socklen_t(MemoryLayout.size(ofValue: addr.sun_family) + pathBytes.count + 1)
        let connectResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPtr in
                Darwin.connect(fd, sockaddrPtr, addrLength)
            }
        }
        guard connectResult == 0 else { return false }

        var payload = Data()
        payload.append(magic)
        var metaLength = UInt32(metadata.count).bigEndian
        var jpegLength = UInt32(jpeg.count).bigEndian
        withUnsafeBytes(of: &metaLength) { payload.append(contentsOf: $0) }
        withUnsafeBytes(of: &jpegLength) { payload.append(contentsOf: $0) }
        payload.append(metadata)
        payload.append(jpeg)

        let writeSucceeded = payload.withUnsafeBytes { rawBuffer -> Bool in
            guard let baseAddress = rawBuffer.baseAddress else { return false }
            var totalSent = 0
            while totalSent < rawBuffer.count {
                let sent = Darwin.write(
                    fd,
                    baseAddress.advanced(by: totalSent),
                    rawBuffer.count - totalSent
                )
                if sent <= 0 {
                    return false
                }
                totalSent += sent
            }
            return true
        }
        guard writeSucceeded else { return false }

        _ = Darwin.shutdown(fd, SHUT_WR)
        var ack: UInt8 = 0
        let ackResult = Darwin.read(fd, &ack, 1)
        return ackResult == 1 && ack == 1
    }
}
