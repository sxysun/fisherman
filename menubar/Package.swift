// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FishermanMenu",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Vendored locally (pinned at pre-@Entry commit c8578b4, 2025-03-26)
        // because upstream HEAD uses the @Entry / #Preview macros, which
        // require the SwiftUIMacros plugin that only ships with full Xcode —
        // not CommandLineTools. We also strip the one #Preview block in
        // the vendored copy so this builds with CLT alone.
        .package(path: "Packages/DynamicNotchKit"),
    ],
    targets: [
        .executableTarget(
            name: "FishermanMenu",
            dependencies: ["DynamicNotchKit"],
            path: "Sources"
        ),
    ]
)
