// 使用系统字体绘制现有“玄”品牌标记，生成可重复构建的 macOS 应用图标。
import AppKit

let image = NSImage(size: NSSize(width: 1024, height: 1024))
image.lockFocus()
NSColor(red: 0.10, green: 0.22, blue: 0.16, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 72, y: 72, width: 880, height: 880), xRadius: 204, yRadius: 204).fill()
NSColor(red: 0.73, green: 0.86, blue: 0.78, alpha: 1).setFill()
NSBezierPath(ovalIn: NSRect(x: 204, y: 204, width: 616, height: 616)).fill()
let font = NSFont(name: "PingFangSC-Medium", size: 380) ?? NSFont.systemFont(ofSize: 380)
let attributes: [NSAttributedString.Key: Any] = [
    .font: font, .foregroundColor: NSColor(red: 0.10, green: 0.22, blue: 0.16, alpha: 1)
]
let text = "玄" as NSString
let size = text.size(withAttributes: attributes)
text.draw(at: NSPoint(x: (1024 - size.width) / 2, y: (1024 - size.height) / 2), withAttributes: attributes)
image.unlockFocus()
guard let data = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: data),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("无法生成应用图标")
}
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
