import Foundation
import SwiftUI

/// Observable state shared with the SwiftUI views inside DynamicNotch.
/// The coordinator drives this; views react to changes via @ObservedObject.
@MainActor
final class HarnessState: ObservableObject {
    @Published var current: PendingPayload?

    /// Coordinator-installed handlers, called from view actions.
    var actionHandler: ((String) -> Void)?
    var hoverHandler: ((String, Bool) -> Void)?
}
