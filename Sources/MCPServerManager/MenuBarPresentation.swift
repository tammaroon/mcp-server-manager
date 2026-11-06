import AppKit

@MainActor enum MenuBarPresentation {
    static let preferredSize = NSSize(width: 468, height: 780)

    static func size(available: NSSize) -> NSSize {
        NSSize(width: min(preferredSize.width, max(1, available.width - 24)),
               height: min(preferredSize.height, max(1, available.height - 24)))
    }

    static func icon() -> NSImage? {
        let image = NSImage(systemSymbolName: "point.3.connected.trianglepath.dotted", accessibilityDescription: "MCP Server Manager")?
            .withSymbolConfiguration(.init(pointSize: 16, weight: .medium))
        image?.isTemplate = true
        image?.size = NSSize(width: 18, height: 18)
        return image
    }
}
