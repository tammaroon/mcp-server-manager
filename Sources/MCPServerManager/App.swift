import AppKit
import Foundation
import SwiftUI

@main enum EntryPoint {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSPopoverDelegate {
    private let model = ManagerModel()
    private var statusItem: NSStatusItem!
    private var popover = NSPopover()
    private var window: NSWindow?
    private var localClickMonitor: Any?
    private var globalClickMonitor: Any?
    private var deactivateObserver: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Keep a single menu-bar instance.
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "local.mcp-server-manager")
        if others.contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) { NSApp.terminate(nil); return }
        installMenus()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = MenuBarPresentation.icon()
            button.contentTintColor = nil
            button.target = self
            button.action = #selector(togglePopover)
            button.toolTip = "MCP Server Manager · upstreams not checked"
        }
        popover.behavior = .transient
        popover.delegate = self
        configurePopover()
        model.statusChanged = { [weak self] in self?.updateStatus() }
        model.start()
        let launchedAtLogin = NSAppleEventManager.shared().currentAppleEvent?.eventID == AEEventID(kAEOpenApplication) && NSAppleEventManager.shared().currentAppleEvent?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue == OSType(keyAELaunchedAsLogInItem)
        if !launchedAtLogin { showManager() }
    }
    private func installMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "MCP Server Manager")
        appMenu.addItem(withTitle: "About MCP Server Manager", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let connections = appMenu.addItem(withTitle: "Show connections", action: #selector(togglePopover), keyEquivalent: "m")
        connections.keyEquivalentModifierMask = [.command, .shift]
        connections.target = self
        let settings = appMenu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settings.target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit MCP Server Manager", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.mainMenu = main
        NSApp.windowsMenu = windowMenu
    }
    @objc private func openSettings() { model.page = "settings"; showManager() }
    private func updateStatus() {
        statusItem.button?.toolTip = "MCP Server Manager · \(model.aggregate.0)"
        // Template rendering follows the menu bar appearance, including its highlight.
        statusItem.button?.contentTintColor = nil
    }
    @objc private func togglePopover() {
        if popover.isShown { popover.performClose(nil) }
        else if let button = statusItem.button {
            configurePopover()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }
    private func configurePopover() {
        let screen = statusItem.button?.window?.screen ?? NSScreen.main
        let size = MenuBarPresentation.size(available: screen?.visibleFrame.size ?? NSSize(width: 1440, height: 900))
        if let controller = popover.contentViewController as? NSHostingController<MenuPopoverView> {
            controller.rootView = menuPopover(size: size)
            controller.preferredContentSize = size
        } else {
            let controller = NSHostingController(rootView: menuPopover(size: size))
            controller.preferredContentSize = size
            popover.contentViewController = controller
        }
        popover.contentSize = size
    }
    private func menuPopover(size: NSSize) -> MenuPopoverView {
        MenuPopoverView(model: model, size: size, openManager: { [weak self] in self?.showManager() }, quit: { NSApp.terminate(nil) })
    }
    func popoverDidShow(_ notification: Notification) {
        removePopoverMonitors()
        let clicks: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        globalClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: clicks) { [weak self] _ in
            self?.popover.performClose(nil)
        }
        localClickMonitor = NSEvent.addLocalMonitorForEvents(matching: clicks.union(.keyDown)) { [weak self] event in
            guard let self, self.popover.isShown else { return event }
            if event.type == .keyDown {
                if event.keyCode == 53 { self.popover.performClose(nil); return nil }
                return event
            }
            // Menus opened from inside the popover belong to its interaction flow.
            if let window = event.window, window.level.rawValue >= NSWindow.Level.popUpMenu.rawValue { return event }
            if event.window !== self.popover.contentViewController?.view.window && event.window !== self.statusItem.button?.window {
                self.popover.performClose(nil)
            }
            return event
        }
        deactivateObserver = NotificationCenter.default.addObserver(forName: NSApplication.didResignActiveNotification, object: NSApp, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.popover.performClose(nil) }
        }
    }
    func popoverDidClose(_ notification: Notification) { removePopoverMonitors() }
    private func removePopoverMonitors() {
        if let localClickMonitor { NSEvent.removeMonitor(localClickMonitor); self.localClickMonitor = nil }
        if let globalClickMonitor { NSEvent.removeMonitor(globalClickMonitor); self.globalClickMonitor = nil }
        if let deactivateObserver { NotificationCenter.default.removeObserver(deactivateObserver); self.deactivateObserver = nil }
    }
    func showManager() {
        popover.performClose(nil)
        if window == nil {
            let controller = NSHostingController(rootView: ManagerView(model: model))
            let manager = NSWindow(contentViewController: controller)
            manager.title = "MCP Server Manager"
            manager.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
            manager.titlebarAppearsTransparent = true
            manager.titleVisibility = .hidden
            manager.backgroundColor = NSColor(Palette.background)
            manager.setContentSize(NSSize(width: 1130, height: 800))
            manager.minSize = NSSize(width: 1060, height: 730)
            manager.isReleasedWhenClosed = false
            manager.delegate = self
            manager.center()
            window = manager
        }
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ notification: Notification) {
        model.timer?.invalidate()
        removePopoverMonitors()
        // The shared daemon stays available to active client bridges when the UI quits.
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showManager(); return true }
}
