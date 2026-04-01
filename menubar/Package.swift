// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FishermanMenu",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/MrKai77/DynamicNotchKit", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "FishermanMenu",
            dependencies: ["DynamicNotchKit"],
            path: "Sources"
        ),
    ]
)
