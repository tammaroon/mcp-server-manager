import AppKit
import SwiftUI

@main enum PopoverLayoutCheck {
    @MainActor static func main() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let controller = NSHostingController(rootView: MenuPopoverView(model: ManagerModel(), openManager: {}, quit: {}))
        controller.view.layoutSubtreeIfNeeded()
        let size = controller.view.fittingSize
        guard abs(size.width - 468) <= 1, size.height >= 700 else {
            FileHandle.standardError.write(Data("FAIL: popover fits at \(size.width) × \(size.height); expected 468 points wide and at least 700 high to avoid clipping and a collapsed server list\n".utf8))
            exit(1)
        }
        let small = MenuBarPresentation.size(available: NSSize(width: 480, height: 650))
        guard small.width <= 456, small.height <= 626 else { fatalError("Popover must fit smaller screens") }
        guard MenuBarPresentation.icon()?.isTemplate == true else { fatalError("Menu-bar icon must adapt to light/dark backgrounds") }
        print("PASS: menu popover reserves \(size.width) × \(size.height) points")
    }
}
