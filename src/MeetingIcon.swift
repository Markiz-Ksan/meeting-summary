import AppKit

/// A vector template remains crisp at either menu-bar backing scale and lets
/// AppKit supply the correct foreground colour for light, dark and selected UI.
enum MeetingIcon {
    static func menuBar() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            context.saveGState()
            defer { context.restoreGState() }
            context.setShouldAntialias(true)
            context.setStrokeColor(NSColor.black.cgColor)
            context.setLineCap(.round)
            context.setLineJoin(.round)
            context.setLineWidth(1.3)

            let bubble = CGMutablePath()
            bubble.move(to: CGPoint(x: 4.2, y: 16.25))
            bubble.addLine(to: CGPoint(x: 13.8, y: 16.25))
            bubble.addQuadCurve(to: CGPoint(x: 16.35, y: 13.7), control: CGPoint(x: 16.35, y: 16.25))
            bubble.addLine(to: CGPoint(x: 16.35, y: 6.7))
            bubble.addQuadCurve(to: CGPoint(x: 13.8, y: 4.15), control: CGPoint(x: 16.35, y: 4.15))
            bubble.addLine(to: CGPoint(x: 7.0, y: 4.15))
            bubble.addLine(to: CGPoint(x: 3.35, y: 1.8))
            bubble.addLine(to: CGPoint(x: 3.85, y: 4.17))
            bubble.addQuadCurve(to: CGPoint(x: 1.65, y: 6.7), control: CGPoint(x: 1.65, y: 4.3))
            bubble.addLine(to: CGPoint(x: 1.65, y: 13.7))
            bubble.addQuadCurve(to: CGPoint(x: 4.2, y: 16.25), control: CGPoint(x: 1.65, y: 16.25))
            bubble.closeSubpath()
            context.addPath(bubble)
            context.strokePath()

            context.setLineWidth(1.25)
            for (y, end) in [(12.7, 13.1), (9.8, 10.5), (6.9, 7.0)] {
                context.move(to: CGPoint(x: 4.65, y: y))
                context.addLine(to: CGPoint(x: end, y: y))
                context.strokePath()
            }
            context.setLineWidth(1.05)
            for (x, height) in [(9.65, 1.1), (11.4, 3.0), (13.15, 1.8)] {
                context.move(to: CGPoint(x: x, y: 6.9 - height / 2))
                context.addLine(to: CGPoint(x: x, y: 6.9 + height / 2))
                context.strokePath()
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "Сводка встречи"
        return image
    }
}
