import Foundation
import SwiftUI

enum HarnessNotchPanel: String, CaseIterable, Identifiable {
    case ping = "Ping"
    case pipeline = "Pipeline"
    case diet = "Diet"
    case settings = "Settings"

    var id: String { rawValue }
}

/// Observable state shared with the SwiftUI views inside the harness surface.
/// The coordinator drives this; views react to changes via @ObservedObject.
@MainActor
final class HarnessState: ObservableObject {
    @Published var current: PendingPayload?
    @Published var activePanel: HarnessNotchPanel = .pipeline
    @Published var surfaceExpanded = false
    @Published var surfacePinned = false

    /// Coordinator-installed handlers, called from view actions.
    var actionHandler: ((String) -> Void)?
    var hoverHandler: ((String, Bool) -> Void)?
    var surfaceHoverHandler: ((Bool) -> Void)?
    var togglePinHandler: (() -> Void)?
    var dragHandler: ((CGSize) -> Void)?
    var dragEndHandler: (() -> Void)?
}
