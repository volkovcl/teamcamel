// Prints the outlines of a line of text in a given font as SVG path data (kerning included).
// usage: swift outline.swift <font name or path to font file> <text> <size> [x of text start] [y of baseline]
// macOS only (CoreText). The font does not have to be installed when a path to its file is given.
import Foundation
import CoreText

let a = CommandLine.arguments
let size = CGFloat(Double(a[3])!)
let dx = a.count > 4 ? CGFloat(Double(a[4])!) : 0, dy = a.count > 5 ? CGFloat(Double(a[5])!) : 0
var font: CTFont
if FileManager.default.fileExists(atPath: a[1]),
   let descs = CTFontManagerCreateFontDescriptorsFromURL(URL(fileURLWithPath: a[1]) as CFURL) as? [CTFontDescriptor], let first = descs.first {
    font = CTFontCreateWithFontDescriptor(first, size, nil)
} else {
    font = CTFontCreateWithName(a[1] as CFString, size, nil)
}
let attr = NSAttributedString(string: a[2], attributes: [NSAttributedString.Key(kCTFontAttributeName as String): font])
let line = CTLineCreateWithAttributedString(attr)
var d = ""
for run in CTLineGetGlyphRuns(line) as! [CTRun] {
    let n = CTRunGetGlyphCount(run)
    var glyphs = [CGGlyph](repeating: 0, count: n), pos = [CGPoint](repeating: .zero, count: n)
    CTRunGetGlyphs(run, CFRange(location: 0, length: n), &glyphs)
    CTRunGetPositions(run, CFRange(location: 0, length: n), &pos)
    for i in 0..<n {
        guard let path = CTFontCreatePathForGlyph(font, glyphs[i], nil) else { continue }
        let ox = pos[i].x, oy = pos[i].y
        path.applyWithBlock { el in
            let p = el.pointee.points
            func f(_ k: Int) -> String { String(format: "%.2f %.2f", dx + ox + p[k].x, dy - (oy + p[k].y)) }
            switch el.pointee.type {
            case .moveToPoint: d += "M" + f(0)
            case .addLineToPoint: d += "L" + f(0)
            case .addQuadCurveToPoint: d += "Q" + f(0) + " " + f(1)
            case .addCurveToPoint: d += "C" + f(0) + " " + f(1) + " " + f(2)
            case .closeSubpath: d += "Z"
            @unknown default: break
            }
        }
    }
}
let width = CTLineGetTypographicBounds(line, nil, nil, nil)
FileHandle.standardError.write("font=\(CTFontCopyPostScriptName(font)) width=\(String(format: "%.2f", width)) capHeight=\(String(format: "%.2f", CTFontGetCapHeight(font)))\n".data(using: .utf8)!)
print(d)
