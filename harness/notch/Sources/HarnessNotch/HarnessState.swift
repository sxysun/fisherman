import Foundation
import SwiftUI

enum HarnessNotchPanel: String, CaseIterable, Identifiable {
    case ping = "Ping"
    case pipeline = "Pipeline"
    case diet = "Diet"

    var id: String { rawValue }
}

/// Observable state shared with the SwiftUI views inside DynamicNotch.
/// The coordinator drives this; views react to changes via @ObservedObject.
@MainActor
final class HarnessState: ObservableObject {
    @Published var current: PendingPayload?
    @Published var activePanel: HarnessNotchPanel = .pipeline

    /// Coordinator-installed handlers, called from view actions.
    var actionHandler: ((String) -> Void)?
    var hoverHandler: ((String, Bool) -> Void)?
}
