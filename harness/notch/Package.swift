// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "HarnessNotch",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "HarnessNotch", targets: ["HarnessNotch"]),
    ],
    dependencies: [
        // Same library FishermanMenu uses — for UI parity.
        .package(path: "../../menubar/Packages/DynamicNotchKit"),
    ],
    targets: [
        .executableTarget(
            name: "HarnessNotch",
            dependencies: [
                .product(name: "DynamicNotchKit", package: "DynamicNotchKit"),
            ],
            path: "Sources/HarnessNotch"
        ),
    ]
)
