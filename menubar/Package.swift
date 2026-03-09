// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "FishermanMenu",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "FishermanMenu", path: "Sources"),
    ]
)
