import CoreGraphics
import Foundation
import ImageIO
import Vision

struct Box: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let confidence: Float
}

struct Label: Codable {
    let name: String
    let confidence: Float
}

struct TextBox: Codable {
    let text: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let confidence: Float
}

struct Result: Codable {
    let path: String
    let people: [Box]
    let faces: [Box]
    let labels: [Label]
    let texts: [TextBox]
    let error: String?
}

func box(_ observation: VNDetectedObjectObservation) -> Box {
    let rect = observation.boundingBox
    return Box(
        x: rect.origin.x,
        y: rect.origin.y,
        width: rect.size.width,
        height: rect.size.height,
        confidence: observation.confidence
    )
}

func recognizedText(
    _ observation: VNRecognizedTextObservation
) -> TextBox? {
    guard let candidate = observation.topCandidates(1).first else {
        return nil
    }
    let value = candidate.string.trimmingCharacters(
        in: .whitespacesAndNewlines
    )
    guard !value.isEmpty, candidate.confidence >= 0.20 else {
        return nil
    }
    let rect = observation.boundingBox
    return TextBox(
        text: value,
        x: rect.origin.x,
        y: rect.origin.y,
        width: rect.size.width,
        height: rect.size.height,
        confidence: candidate.confidence
    )
}

func analyze(_ path: String) -> Result {
    let url = URL(fileURLWithPath: path)
    guard
        let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        return Result(
            path: path,
            people: [],
            faces: [],
            labels: [],
            texts: [],
            error: "decode_failed"
        )
    }

    let classify = VNClassifyImageRequest()
    let humans = VNDetectHumanRectanglesRequest()
    humans.upperBodyOnly = false
    let faces = VNDetectFaceRectanglesRequest()
    let text = VNRecognizeTextRequest()
    text.recognitionLevel = .accurate
    text.usesLanguageCorrection = true
    text.automaticallyDetectsLanguage = true
    text.minimumTextHeight = 0.012
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([classify, humans, faces, text])
        let people = (humans.results ?? []).map { box($0) }
        let faceBoxes = (faces.results ?? []).map { box($0) }
        let labels = (classify.results ?? []).prefix(50).map {
            Label(name: $0.identifier, confidence: $0.confidence)
        }
        let texts = (text.results ?? []).compactMap {
            recognizedText($0)
        }
        return Result(
            path: path,
            people: people,
            faces: faceBoxes,
            labels: labels,
            texts: texts,
            error: nil
        )
    } catch {
        return Result(
            path: path,
            people: [],
            faces: [],
            labels: [],
            texts: [],
            error: String(describing: error)
        )
    }
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
let arguments = Array(CommandLine.arguments.dropFirst())
var paths = arguments
var outputPath: String?
if arguments.count >= 2 && arguments[0] == "--output" {
    outputPath = arguments[1]
    paths = Array(arguments.dropFirst(2))
}
if paths.count == 2 && paths[0] == "--directory" {
    let directory = paths[1]
    let enumerator = FileManager.default.enumerator(atPath: directory)
    paths = (enumerator?.allObjects as? [String] ?? [])
        .filter {
            let ext = URL(fileURLWithPath: $0).pathExtension.lowercased()
            return ["jpg", "jpeg", "png", "webp", "avif"].contains(ext)
        }
        .map { URL(fileURLWithPath: directory).appendingPathComponent($0).path }
        .sorted()
}

var lines: [String] = []
for path in paths {
    if let data = try? encoder.encode(analyze(path)),
       let line = String(data: data, encoding: .utf8) {
        lines.append(line)
    }
}
let output = lines.joined(separator: "\n") + (lines.isEmpty ? "" : "\n")
if let outputPath {
    try output.write(toFile: outputPath, atomically: true, encoding: .utf8)
} else {
    print(output, terminator: "")
}
