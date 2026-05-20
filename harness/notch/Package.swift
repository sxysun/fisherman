// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HarnessNotch",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "HarnessNotch", targets: ["HarnessNotch"]),
    ],
    dependencies: [],
    targets: [
        .executableTarget(
            name: "HarnessNotch",
            dependencies: [],
            path: "Sources/HarnessNotch"
        ),
    ]
)
