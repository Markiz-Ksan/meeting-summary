import AppKit

// Reproducible vector artwork: all representations are drawn at their native
// pixel size, retaining a transparent margin around the macOS app tile.
let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "assets")
let iconset = output.appendingPathComponent("MeetingOverlay.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: red, green: green, blue: blue, alpha: alpha)
}

func renderIcon(size: Int) throws -> Data {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    let context = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8,
                            bytesPerRow: size * 4, space: space,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    context.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)

    let tile = CGPath(roundedRect: CGRect(x: 72, y: 72, width: 880, height: 880),
                      cornerWidth: 198, cornerHeight: 198, transform: nil)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -12), blur: 20,
                      color: color(0.02, 0.09, 0.20, 0.28))
    context.addPath(tile)
    context.setFillColor(color(0.06, 0.35, 0.71))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    context.addPath(tile)
    context.clip()
    let gradient = CGGradient(colorsSpace: space,
                              colors: [color(0.08, 0.43, 0.91), color(0.02, 0.59, 0.66)] as CFArray,
                              locations: [0, 1])!
    context.drawLinearGradient(gradient, start: CGPoint(x: 160, y: 920),
                               end: CGPoint(x: 840, y: 80), options: [])
    // A restrained top highlight gives the tile depth without small details.
    let highlight = CGGradient(colorsSpace: space,
                               colors: [color(1, 1, 1, 0.17), color(1, 1, 1, 0)] as CFArray,
                               locations: [0, 1])!
    context.drawLinearGradient(highlight, start: CGPoint(x: 0, y: 950),
                               end: CGPoint(x: 0, y: 460), options: [])
    context.restoreGState()

    let bubble = CGMutablePath()
    bubble.move(to: CGPoint(x: 310, y: 780))
    bubble.addLine(to: CGPoint(x: 714, y: 780))
    bubble.addCurve(to: CGPoint(x: 814, y: 680), control1: CGPoint(x: 774, y: 780), control2: CGPoint(x: 814, y: 740))
    bubble.addLine(to: CGPoint(x: 814, y: 382))
    bubble.addCurve(to: CGPoint(x: 714, y: 282), control1: CGPoint(x: 814, y: 322), control2: CGPoint(x: 774, y: 282))
    bubble.addLine(to: CGPoint(x: 448, y: 282))
    bubble.addLine(to: CGPoint(x: 302, y: 198))
    bubble.addCurve(to: CGPoint(x: 280, y: 216), control1: CGPoint(x: 286, y: 188), control2: CGPoint(x: 276, y: 198))
    bubble.addLine(to: CGPoint(x: 296, y: 284))
    bubble.addCurve(to: CGPoint(x: 210, y: 382), control1: CGPoint(x: 244, y: 288), control2: CGPoint(x: 210, y: 327))
    bubble.addLine(to: CGPoint(x: 210, y: 680))
    bubble.addCurve(to: CGPoint(x: 310, y: 780), control1: CGPoint(x: 210, y: 740), control2: CGPoint(x: 250, y: 780))
    bubble.closeSubpath()

    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -10), blur: 20,
                      color: color(0, 0.15, 0.35, 0.20))
    context.addPath(bubble)
    context.setFillColor(color(0.98, 1, 1))
    context.fillPath()
    context.restoreGState()

    context.setStrokeColor(color(0.06, 0.34, 0.57))
    context.setLineWidth(44)
    context.setLineCap(.round)
    for (y, endX) in [(CGFloat(648), CGFloat(716)), (CGFloat(536), CGFloat(644)), (CGFloat(424), CGFloat(500))] {
        context.move(to: CGPoint(x: 310, y: y))
        context.addLine(to: CGPoint(x: endX, y: y))
        context.strokePath()
    }

    // A compact waveform links the concise text to audio. Omit it only in the
    // 16 px bitmap, where a clean summary silhouette is easier to recognise.
    if size >= 32 {
        context.setStrokeColor(color(0.02, 0.57, 0.64))
        context.setLineWidth(22)
        for (x, height) in [(CGFloat(594), CGFloat(28)), (CGFloat(628), CGFloat(68)),
                            (CGFloat(662), CGFloat(100)), (CGFloat(696), CGFloat(54))] {
            context.move(to: CGPoint(x: x, y: 424 - height / 2))
            context.addLine(to: CGPoint(x: x, y: 424 + height / 2))
            context.strokePath()
        }
    }

    let image = context.makeImage()!
    let bitmap = NSBitmapImageRep(cgImage: image)
    return bitmap.representation(using: .png, properties: [:])!
}

for logicalSize in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let suffix = scale == 1 ? "" : "@2x"
        let name = "icon_\(logicalSize)x\(logicalSize)\(suffix).png"
        try renderIcon(size: logicalSize * scale).write(to: iconset.appendingPathComponent(name))
    }
}
try renderIcon(size: 1024).write(to: output.appendingPathComponent("MeetingOverlay-preview.png"))

let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = ["-c", "icns", iconset.path, "-o", output.appendingPathComponent("MeetingOverlay.icns").path]
try task.run()
task.waitUntilExit()
guard task.terminationStatus == 0 else {
    FileHandle.standardError.write(Data("iconutil could not package the icon; macOS may require running this build outside the tool sandbox. PNGs remain in \(iconset.path).\n".utf8))
    exit(EXIT_FAILURE)
}
print("Created MeetingOverlay.icns and 10 PNG representations in \(output.path)")
