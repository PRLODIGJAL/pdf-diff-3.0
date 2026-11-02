import Foundation
import Vision
import AppKit

// 用法: vision_ocr <img> [json]
// recognitionLanguages 只是**候选池**；automaticallyDetectsLanguage = true 让 Vision
// 对每一行自动挑语言 —— 语言列表是有优先级的链，zh-Hans 排前面会把西里尔字母
// 按汉字猜（2026-09-30 俄文标签 GRA-3634 实测：ru-RU 单列好、三列混排垃圾；
// 开自动检测后俄文中文都好）。候选池可按需加语言（如 uk/de/fr…），加了不影响
// 已支持语言的表现（zh 两列/三列输出逐字一致，实测）。
//
// 第二个参数 = "json" 时输出**行级** JSON 数组（text + boundingBox 归一化坐标、
// 原点左下）—— 扫描件「整页 OCR 行对拍」管线用（GRA-3925 两张不同产品的
// 标签，像素候选全灭，靠两版各自的行级文字层对拍）。不传参数保持旧行为。
let args = CommandLine.arguments
guard args.count > 1, let img = NSImage(contentsOfFile: args[1]),
      let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let cg = rep.cgImage else { FileHandle.standardError.write("bad image\n".data(using:.utf8)!); exit(1) }
let wantJSON = args.count > 2 && args[2] == "json"
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.automaticallyDetectsLanguage = true
req.recognitionLanguages = ["zh-Hans", "en-US", "ru-RU"]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try? handler.perform([req])
if wantJSON {
    var lines: [[String: Any]] = []
    for obs in (req.results ?? []) {
        if let c = obs.topCandidates(1).first {
            let bb = obs.boundingBox
            lines.append(["t": c.string, "x": bb.origin.x, "y": bb.origin.y,
                          "w": bb.width, "h": bb.height])
        }
    }
    if let data = try? JSONSerialization.data(withJSONObject: lines),
       let s = String(data: data, encoding: .utf8) {
        print(s)
    }
} else {
    for obs in (req.results ?? []) {
        if let c = obs.topCandidates(1).first { print(c.string) }
    }
}
