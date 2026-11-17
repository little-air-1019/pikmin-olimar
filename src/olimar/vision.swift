// olimar 原生 helper（macOS）。
//   olimar-vision ocr <png>                       一次性 OCR（accurate）
//   olimar-vision colors <png> x y w h            一次性顏色統計
//   olimar-vision serve <bundle-id>               常駐 daemon：stdin/stdout 一行一個 JSON 指令
//
// daemon 指令：info / focus / restore / shot / colors / crop / click / drag
// 點擊 guard 全在 daemon 內、送出事件前一刻執行：視窗仍存在且 bounds 與截圖時相同、pid＝目標 app、
// 目標 app 為 frontmost、點在視窗內、該點最上層視窗就是目標視窗、截圖不過舊。任何一項失敗都不送事件。
import Foundation
import Vision
import CoreGraphics
import ImageIO
import AppKit
import ScreenCaptureKit
import UniformTypeIdentifiers

func nowMs() -> Double { Date().timeIntervalSince1970 * 1000 }
func out(_ obj: Any) {
    let d = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(d + Data("\n".utf8))
}
func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}
func loadPNG(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}
func writePNG(_ img: CGImage, _ path: String) -> Bool {
    guard let d = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, UTType.png.identifier as CFString, 1, nil) else { return false }
    CGImageDestinationAddImage(d, img, nil)
    return CGImageDestinationFinalize(d)
}

// ---------- OCR ----------
func runOCR(_ img: CGImage, roi: CGRect?, fast: Bool) throws -> [[String: Any]] {
    var target = img, ox = 0, oy = 0
    if let r = roi {
        let c = r.integral.intersection(CGRect(x: 0, y: 0, width: img.width, height: img.height))
        guard !c.isEmpty, let cropped = img.cropping(to: c) else { return [] }
        target = cropped; ox = Int(c.minX); oy = Int(c.minY)
    }
    let W = target.width, H = target.height
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fast ? .fast : .accurate
    req.recognitionLanguages = ["zh-Hant", "en-US"]
    req.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: target, options: [:]).perform([req])
    var lines: [[String: Any]] = []
    for o in (req.results ?? []) {
        guard let c = o.topCandidates(1).first else { continue }
        let b = o.boundingBox
        lines.append([
            "text": c.string, "conf": Double(c.confidence),
            "x": ox + Int((b.minX * Double(W)).rounded()),
            "y": oy + Int(((1 - b.maxY) * Double(H)).rounded()),
            "w": Int((b.width * Double(W)).rounded()),
            "h": Int((b.height * Double(H)).rounded()),
        ])
    }
    return lines
}

// ---------- 顏色統計 ----------
func colorStats(_ img: CGImage, _ rx: Int, _ ry: Int, _ rw: Int, _ rh: Int) -> [String: Any] {
    let W = img.width, H = img.height
    let x0 = max(0, rx), y0 = max(0, ry), x1 = min(W, rx + rw), y1 = min(H, ry + rh)
    guard x1 > x0, y1 > y0 else { return ["total": 0] }
    let w = x1 - x0, h = y1 - y0
    var buf = [UInt8](repeating: 0, count: w * h * 4)
    let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.draw(img, in: CGRect(x: -x0, y: -(H - y1), width: W, height: H))
    var c = ["orange": 0, "red": 0, "yellow": 0, "green": 0, "blue": 0, "purple": 0, "white": 0, "dark": 0]
    var sr = 0.0, sg = 0.0, sb = 0.0
    for i in 0..<(w * h) {
        let r = Double(buf[i * 4]), g = Double(buf[i * 4 + 1]), b = Double(buf[i * 4 + 2])
        sr += r; sg += g; sb += b
        let mx = max(r, g, b), mn = min(r, g, b)
        if mx < 60 { c["dark"]! += 1; continue }
        let sat = (mx - mn) / mx
        if mn > 225 && sat < 0.12 { c["white"]! += 1; continue }
        if sat > 0.5 && mx > 150 {
            var hue = 0.0
            if mx != mn {
                if mx == r { hue = 60 * (((g - b) / (mx - mn)).truncatingRemainder(dividingBy: 6)) }
                else if mx == g { hue = 60 * ((b - r) / (mx - mn) + 2) }
                else { hue = 60 * ((r - g) / (mx - mn) + 4) }
                if hue < 0 { hue += 360 }
            }
            if hue >= 15 && hue < 45 { c["orange"]! += 1 }
            else if hue >= 45 && hue < 70 { c["yellow"]! += 1 }
            else if hue < 15 || hue >= 340 { c["red"]! += 1 }
            else if hue >= 70 && hue < 170 { c["green"]! += 1 }
            else if hue >= 190 && hue < 260 { c["blue"]! += 1 }
            else if hue >= 260 && hue < 340 { c["purple"]! += 1 }
        }
    }
    let n = Double(w * h)
    var d: [String: Any] = ["total": w * h, "mean": [Int(sr / n), Int(sg / n), Int(sb / n)]]
    for (k, v) in c { d[k] = Double(v) / n }
    return d
}

/// 把 rect（像素，左上原點）縮放渲染成 w×h 的 RGBA（面積平均）。
func render(_ img: CGImage, _ rx: Int, _ ry: Int, _ rw: Int, _ rh: Int, _ w: Int, _ h: Int) -> [UInt8] {
    let W = img.width, H = img.height
    var buf = [UInt8](repeating: 0, count: w * h * 4)
    guard rw > 0, rh > 0, w > 0, h > 0 else { return buf }
    let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.interpolationQuality = .high
    let sx = Double(w) / Double(rw), sy = Double(h) / Double(rh)
    ctx.draw(img, in: CGRect(x: -Double(rx) * sx, y: -Double(H - (ry + rh)) * sy, width: Double(W) * sx, height: Double(H) * sy))
    return buf
}

// ---------- 視窗／事件 ----------
struct Win { var id: Int; var pid: Int; var x: Double; var y: Double; var w: Double; var h: Double }
func onScreenWindows() -> [[String: Any]] {
    (CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]]) ?? []
}
func winOf(_ d: [String: Any]) -> Win? {
    guard let id = d[kCGWindowNumber as String] as? Int, let pid = d[kCGWindowOwnerPID as String] as? Int,
          let b = d[kCGWindowBounds as String] as? [String: Any],
          let x = (b["X"] as? NSNumber)?.doubleValue, let y = (b["Y"] as? NSNumber)?.doubleValue,
          let w = (b["Width"] as? NSNumber)?.doubleValue, let h = (b["Height"] as? NSNumber)?.doubleValue else { return nil }
    return Win(id: id, pid: pid, x: x, y: y, w: w, h: h)
}
func winDict(_ w: Win) -> [String: Any] { ["id": w.id, "pid": w.pid, "x": w.x, "y": w.y, "w": w.w, "h": w.h] }

final class Daemon {
    let bundle: String
    var pid: Int = 0
    var scWindows: [Int: SCWindow] = [:]
    struct Shot { let img: CGImage; let win: Win; let t: Double }
    var shots: [Int: Shot] = [:]
    var nextShot = 1
    var savedCursor: CGPoint? = nil
    var prevFront: NSRunningApplication? = nil

    init(bundle: String) { self.bundle = bundle }

    func refreshPid() throws {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first else { throw E("app 未執行: \(bundle)") }
        pid = Int(app.processIdentifier)
    }
    struct E: Error { let m: String; init(_ m: String) { self.m = m } }

    /// 目標 app 的主視窗（layer 0、最大者），或指定 id。
    func window(id: Int? = nil) throws -> Win {
        try refreshPid()
        let ws = onScreenWindows().compactMap { d -> Win? in
            guard let w = winOf(d), w.pid == pid, (d[kCGWindowLayer as String] as? Int) == 0, w.w > 100 else { return nil }
            return w
        }
        if let id = id { guard let w = ws.first(where: { $0.id == id }) else { throw E("視窗 \(id) 不存在或不在螢幕上") }; return w }
        guard let w = ws.max(by: { $0.w * $0.h < $1.w * $1.h }) else { throw E("找不到目標視窗") }
        return w
    }
    /// 最上層的一般視窗（layer 0）所屬 pid。NSWorkspace.frontmostApplication 在沒有 runloop 的 daemon 內會過期，所以改看視窗順序。
    func frontmostPid() -> Int {
        for d in onScreenWindows() where (d[kCGWindowLayer as String] as? Int) == 0 && (d[kCGWindowAlpha as String] as? Double ?? 1) > 0.05 {
            if let w = winOf(d), w.w > 50 { return w.pid }
        }
        return 0
    }

    func sync<T>(_ f: @escaping () async throws -> T) throws -> T {
        var result: Result<T, Error>!
        let sem = DispatchSemaphore(value: 0)
        Task { do { result = .success(try await f()) } catch { result = .failure(error) }; sem.signal() }
        sem.wait()
        return try result.get()
    }

    func capture(_ w: Win) throws -> CGImage {
        let sc: SCWindow
        if let c = scWindows[w.id], abs(c.frame.width - w.w) < 0.5, abs(c.frame.height - w.h) < 0.5 { sc = c }
        else {
            let content = try sync { try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true) }
            guard let f = content.windows.first(where: { Int($0.windowID) == w.id }) else { throw E("SCK 找不到視窗 \(w.id)") }
            scWindows[w.id] = f; sc = f
        }
        let filter = SCContentFilter(desktopIndependentWindow: sc)
        let cfg = SCStreamConfiguration()
        cfg.width = Int(w.w * 2); cfg.height = Int(w.h * 2); cfg.showsCursor = false
        return try sync { try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) }
    }

    func handle(_ r: [String: Any]) throws -> [String: Any] {
        let t0 = nowMs()
        func num(_ k: String) -> Double? { (r[k] as? NSNumber)?.doubleValue }
        switch r["cmd"] as? String ?? "" {
        case "info":
            let w = try window()
            return ["win": winDict(w), "frontmost_pid": frontmostPid(), "ax": AXIsProcessTrusted(), "screen": CGPreflightScreenCaptureAccess()]
        case "shot":
            let w = try window(id: (r["win"] as? NSNumber)?.intValue)
            let tc = nowMs()
            let img = try capture(w)
            let capMs = nowMs() - tc
            let id = nextShot; nextShot += 1
            shots[id] = Shot(img: img, win: w, t: nowMs())
            if shots.count > 8 { shots.removeValue(forKey: shots.keys.min()!) }
            var res: [String: Any] = ["shot": id, "width": img.width, "height": img.height, "win": winDict(w),
                                      "frontmost_pid": frontmostPid(), "capture_ms": capMs]
            let mode = r["ocr"] as? String ?? "none"
            if mode != "none" {
                var rois: [CGRect?] = [nil]
                if let list = r["rois_frac"] as? [[NSNumber]] {
                    let W = Double(img.width), H = Double(img.height)
                    rois = list.filter { $0.count == 4 }.map { a in
                        CGRect(x: a[0].doubleValue * W, y: a[1].doubleValue * H, width: a[2].doubleValue * W, height: a[3].doubleValue * H)
                    }
                }
                let to = nowMs()
                var all: [[String: Any]] = []
                for roi in rois { all += try runOCR(img, roi: roi, fast: mode == "fast") }
                res["lines"] = all
                res["ocr_ms"] = nowMs() - to
            }
            if let p = r["save"] as? String { _ = writePNG(img, p) }
            return res
        case "colors":
            guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1], let a = r["rect"] as? [NSNumber], a.count == 4 else { throw E("colors: 需要 shot 與 rect") }
            return colorStats(s.img, a[0].intValue, a[1].intValue, a[2].intValue, a[3].intValue)
        case "row":  // 水平帶狀區域切成 n 格，回傳每格平均 RGB
            guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1], let a = r["rect"] as? [NSNumber], a.count == 4,
                  let n = (r["n"] as? NSNumber)?.intValue, n > 0, n <= 512 else { throw E("row 參數錯誤") }
            let b = render(s.img, a[0].intValue, a[1].intValue, a[2].intValue, a[3].intValue, n, 1)
            var cells: [[Int]] = []
            for i in 0..<n { cells.append([Int(b[i * 4]), Int(b[i * 4 + 1]), Int(b[i * 4 + 2])]) }
            return ["cells": cells]
        case "sig":  // 區域縮成 n×n 灰階，供變化偵測
            guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1], let a = r["rect"] as? [NSNumber], a.count == 4 else { throw E("sig 參數錯誤") }
            let n = (r["n"] as? NSNumber)?.intValue ?? 16
            let b = render(s.img, a[0].intValue, a[1].intValue, a[2].intValue, a[3].intValue, n, n)
            var sig: [Int] = []
            for i in 0..<(n * n) { sig.append((Int(b[i * 4]) + Int(b[i * 4 + 1]) + Int(b[i * 4 + 2])) / 3) }
            return ["sig": sig]
        case "save":
            guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1], let p = r["path"] as? String else { throw E("save 參數錯誤") }
            return ["ok": writePNG(s.img, p)]
        case "crop":
            guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1], let a = r["rect"] as? [NSNumber], a.count == 4,
                  let p = r["path"] as? String, let c = s.img.cropping(to: CGRect(x: a[0].doubleValue, y: a[1].doubleValue, width: a[2].doubleValue, height: a[3].doubleValue)) else { throw E("crop 參數錯誤") }
            return ["ok": writePNG(c, p)]
        case "focus":
            try refreshPid()
            if savedCursor == nil { savedCursor = CGEvent(source: nil)?.location }
            if frontmostPid() == pid { return ["was_front": true, "front": true] }
            prevFront = NSRunningApplication(processIdentifier: pid_t(frontmostPid()))
            NSRunningApplication(processIdentifier: pid_t(pid))?.activate(options: [])
            var waited = 0.0
            while frontmostPid() != pid && waited < 1200 { Thread.sleep(forTimeInterval: 0.02); waited += 20 }
            return ["was_front": false, "front": frontmostPid() == pid, "waited_ms": waited]
        case "restore":
            if let c = savedCursor { CGWarpMouseCursorPosition(c); savedCursor = nil }
            if let p = prevFront { p.activate(options: []); prevFront = nil }
            return ["restored": true]
        case "click", "drag":
            let (gx, gy, cur) = try guardPoint(r, keyX: "x", keyY: "y")
            let src = CGEventSource(stateID: .hidSystemState)
            let mv = num("move_ms") ?? 12, hold = num("hold_ms") ?? 25
            if r["cmd"] as? String == "click" {
                let p = CGPoint(x: gx, y: gy)
                CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)!.post(tap: .cghidEventTap)
                Thread.sleep(forTimeInterval: mv / 1000)
                CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)!.post(tap: .cghidEventTap)
                Thread.sleep(forTimeInterval: hold / 1000)
                CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)!.post(tap: .cghidEventTap)
            } else {
                guard let x2 = num("x2"), let y2 = num("y2") else { throw E("drag 需要 x2,y2") }
                let (gx2, gy2, _) = try guardPoint(["x": x2, "y": y2, "shot": r["shot"] as Any, "max_age_ms": r["max_age_ms"] as Any], keyX: "x", keyY: "y")
                let p0 = CGPoint(x: gx, y: gy)
                CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: p0, mouseButton: .left)!.post(tap: .cghidEventTap)
                Thread.sleep(forTimeInterval: 0.02)
                CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p0, mouseButton: .left)!.post(tap: .cghidEventTap)
                let steps = 14
                for i in 1...steps {
                    let f = Double(i) / Double(steps)
                    let p = CGPoint(x: gx + (gx2 - gx) * f, y: gy + (gy2 - gy) * f)
                    CGEvent(mouseEventSource: src, mouseType: .leftMouseDragged, mouseCursorPosition: p, mouseButton: .left)!.post(tap: .cghidEventTap)
                    Thread.sleep(forTimeInterval: 0.012)
                }
                CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: CGPoint(x: gx2, y: gy2), mouseButton: .left)!.post(tap: .cghidEventTap)
            }
            return ["win": winDict(cur), "global": [gx, gy], "post_ms": nowMs() - t0]
        default:
            throw E("未知指令")
        }
    }

    /// 送出任何滑鼠事件前的 guard。回傳全域座標。
    func guardPoint(_ r: [String: Any], keyX: String, keyY: String) throws -> (Double, Double, Win) {
        guard let px = (r[keyX] as? NSNumber)?.doubleValue, let py = (r[keyY] as? NSNumber)?.doubleValue else { throw E("guard: 缺座標") }
        guard let s = shots[(r["shot"] as? NSNumber)?.intValue ?? -1] else { throw E("guard: 沒有對應截圖，拒絕點擊") }
        let maxAge = (r["max_age_ms"] as? NSNumber)?.doubleValue ?? 3000
        if nowMs() - s.t > maxAge { throw E("guard: 截圖過舊 \(Int(nowMs() - s.t))ms > \(Int(maxAge))ms") }
        let cur = try window(id: s.win.id)
        if cur.x != s.win.x || cur.y != s.win.y || cur.w != s.win.w || cur.h != s.win.h { throw E("guard: 視窗 bounds 已改變") }
        if frontmostPid() != pid { throw E("guard: 目標 app 不是 frontmost（pid \(frontmostPid())）") }
        if !(px > 0 && px < cur.w && py > 0 && py < cur.h) { throw E("guard: 點擊超出視窗 bounds") }
        let gx = cur.x + px, gy = cur.y + py
        for d in onScreenWindows() {  // 由前到後；第一個涵蓋該點的可見視窗必須是目標視窗
            // 只把一般視窗（layer 0）當遮擋物：Dock／桌面等全螢幕透明 overlay 是 layer≠0，不算。
            guard let w = winOf(d), (d[kCGWindowLayer as String] as? Int) == 0, (d[kCGWindowAlpha as String] as? Double ?? 1) > 0.05 else { continue }
            if gx >= w.x && gx < w.x + w.w && gy >= w.y && gy < w.y + w.h {
                if w.id != cur.id { throw E("guard: 該點被其他視窗遮住（\(d[kCGWindowOwnerName as String] as? String ?? "?")）") }
                break
            }
        }
        return (gx, gy, cur)
    }

    func serve() {
        while let line = readLine() {
            guard let d = line.data(using: .utf8), let r = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
                out(["ok": false, "error": "bad json"]); continue
            }
            let t0 = nowMs()
            do {
                var res = try handle(r)
                res["ok"] = true; res["ms"] = nowMs() - t0
                out(res)
            } catch let e as E { out(["ok": false, "error": e.m, "ms": nowMs() - t0]) }
            catch { out(["ok": false, "error": "\(error)", "ms": nowMs() - t0]) }
        }
    }
}

// ---------- main ----------
let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: olimar-vision ocr <png> | colors <png> x y w h | serve <bundle>") }
switch args[1] {
case "ocr":
    guard let img = loadPNG(args[2]) else { fail("讀不到 \(args[2])") }
    do { out(["width": img.width, "height": img.height, "lines": try runOCR(img, roi: nil, fast: false)]) } catch { fail("vision failed: \(error)") }
case "colors":
    guard args.count == 7, let img = loadPNG(args[2]), let x = Int(args[3]), let y = Int(args[4]), let w = Int(args[5]), let h = Int(args[6]) else { fail("bad args") }
    out(colorStats(img, x, y, w, h))
case "row":  // row <png> x y w h n
    guard args.count == 8, let img = loadPNG(args[2]), let x = Int(args[3]), let y = Int(args[4]), let w = Int(args[5]), let h = Int(args[6]), let n = Int(args[7]), n > 0 else { fail("bad args") }
    let b = render(img, x, y, w, h, n, 1)
    var cells: [[Int]] = []
    for i in 0..<n { cells.append([Int(b[i * 4]), Int(b[i * 4 + 1]), Int(b[i * 4 + 2])]) }
    out(["cells": cells])
case "serve":
    Daemon(bundle: args[2]).serve()
default:
    fail("bad args")
}
