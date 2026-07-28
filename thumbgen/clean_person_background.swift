import CoreImage
import CoreVideo
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

enum CleanerError: Error {
    case usage
    case decodeFailed
    case personNotFound
    case outputFailed
}

guard CommandLine.arguments.count == 3 else {
    throw CleanerError.usage
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
guard let input = CIImage(
    contentsOf: inputURL,
    options: [.applyOrientationProperty: true]
) else {
    throw CleanerError.decodeFailed
}

let request = VNGeneratePersonSegmentationRequest()
request.qualityLevel = .accurate
request.outputPixelFormat = kCVPixelFormatType_OneComponent8
let handler = VNImageRequestHandler(ciImage: input, options: [:])
try handler.perform([request])
guard let observation = request.results?.first else {
    throw CleanerError.personNotFound
}

let rawMask = CIImage(cvPixelBuffer: observation.pixelBuffer)
let scaleX = input.extent.width / rawMask.extent.width
let scaleY = input.extent.height / rawMask.extent.height
let mask = rawMask
    .transformed(by: CGAffineTransform(scaleX: scaleX, y: scaleY))
    .cropped(to: input.extent)

let background = CIImage(
    color: CIColor(
        red: 0.94,
        green: 0.91,
        blue: 0.86,
        alpha: 1.0
    )
).cropped(to: input.extent)

guard let filter = CIFilter(name: "CIBlendWithMask") else {
    throw CleanerError.outputFailed
}
filter.setValue(input, forKey: kCIInputImageKey)
filter.setValue(background, forKey: kCIInputBackgroundImageKey)
filter.setValue(mask, forKey: kCIInputMaskImageKey)
guard let output = filter.outputImage?.cropped(to: input.extent) else {
    throw CleanerError.outputFailed
}

let context = CIContext(options: [.useSoftwareRenderer: false])
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
try context.writePNGRepresentation(
    of: output,
    to: outputURL,
    format: .RGBA8,
    colorSpace: colorSpace
)
