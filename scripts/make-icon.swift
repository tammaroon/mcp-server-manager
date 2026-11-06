import AppKit
let directory = CommandLine.arguments[1]
let fm = FileManager.default
try fm.createDirectory(atPath: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        bitmap.size = NSSize(width: pixels, height: pixels)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let s = CGFloat(pixels)
        let rect = NSRect(x: s * 0.045, y: s * 0.045, width: s * 0.91, height: s * 0.91)
        let box = NSBezierPath(roundedRect: rect, xRadius: s * 0.205, yRadius: s * 0.205)
        NSGradient(starting: NSColor(calibratedRed: 0.56, green: 0.65, blue: 1, alpha: 1), ending: NSColor(calibratedRed: 0.22, green: 0.30, blue: 0.67, alpha: 1))!.draw(in: box, angle: -55)
        let points = [NSPoint(x: s * 0.28, y: s * 0.34), NSPoint(x: s * 0.72, y: s * 0.34), NSPoint(x: s * 0.50, y: s * 0.73)]
        let path = NSBezierPath()
        path.move(to: points[0]); path.line(to: points[2]); path.line(to: points[1]); path.close()
        path.lineWidth = s * 0.048
        path.lineJoinStyle = .round
        NSColor.white.withAlphaComponent(0.80).setStroke(); path.stroke()
        for point in points {
            let circle = NSBezierPath(ovalIn: NSRect(x: point.x - s * 0.075, y: point.y - s * 0.075, width: s * 0.15, height: s * 0.15))
            NSColor(calibratedRed: 0.32, green: 0.41, blue: 0.78, alpha: 1).setFill(); circle.fill()
            circle.lineWidth = s * 0.035; NSColor.white.setStroke(); circle.stroke()
        }
        NSGraphicsContext.restoreGraphicsState()
        let suffix = scale == 2 ? "@2x" : ""
        let pathOut = "\(directory)/icon_\(size)x\(size)\(suffix).png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: pathOut))
    }
}
