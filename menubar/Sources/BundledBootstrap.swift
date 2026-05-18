import Foundation

enum BundledBootstrap {
    static func ensureInstall() {
        guard let resourcePath = Bundle.main.resourcePath else { return }

        let sourceDir = (resourcePath as NSString).appendingPathComponent("fisherman-source")
        let scriptPath = (resourcePath as NSString).appendingPathComponent("bootstrap-user-install.sh")
        let releasePath = (resourcePath as NSString).appendingPathComponent("fisherman-release.json")

        let fm = FileManager.default
        guard fm.fileExists(atPath: sourceDir + "/pyproject.toml"),
              fm.fileExists(atPath: sourceDir + "/fisherman/daemon.py"),
              fm.fileExists(atPath: scriptPath)
        else {
            return
        }

        let installDir = NSHomeDirectory() + "/.fisherman"
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [scriptPath, sourceDir, installDir, releasePath]
        proc.environment = buildEnvironment()

        let logDir = installDir + "/logs"
        try? fm.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let logPath = logDir + "/bootstrap-launch.log"
        if !fm.fileExists(atPath: logPath) {
            fm.createFile(atPath: logPath, contents: nil)
        }

        let logHandle = FileHandle(forWritingAtPath: logPath) ?? .nullDevice
        logHandle.seekToEndOfFile()
        proc.standardOutput = logHandle
        proc.standardError = logHandle

        do {
            NSLog("[Fisherman] ensuring bundled install at \(installDir)")
            try proc.run()
            proc.waitUntilExit()
            if proc.terminationStatus != 0 {
                NSLog("[Fisherman] bundled install failed with code \(proc.terminationStatus); see \(logPath)")
            }
        } catch {
            NSLog("[Fisherman] failed to run bundled install: \(error)")
        }
    }
}
