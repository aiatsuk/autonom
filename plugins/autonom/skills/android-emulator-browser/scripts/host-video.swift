// Completed, owned RGBA/BGRA frames in; scrcpy-compatible H.264 out.
// Input: 32-byte big-endian header: magic AF01, payload bytes, width, height,
// stride, pixel format (1 RGBA, 5 BGRA, 17 bottom-up RGBA), target width and height (UInt32 each).
// A zero-payload header with all fields zero except magic requests an IDR.
// No shared mappings: a frame is validated and fully read before encoding.
import Foundation
import Darwin
import VideoToolbox
import CoreMedia
import CoreVideo
import Accelerate

signal(SIGPIPE, SIG_IGN)
func fail(_ message: String, _ code: Int32 = 2) -> Never {
    fputs(message + "\n", stderr); exit(code)
}
func readExact(_ count: Int, allowEOF: Bool = false) -> Data? {
    var data = Data(count: count)
    var offset = 0
    data.withUnsafeMutableBytes { raw in
        while offset < count {
            let n = read(STDIN_FILENO, raw.baseAddress!.advanced(by: offset), count-offset)
            if n < 0 && errno == EINTR { continue }
            if n == 0 && offset == 0 && allowEOF { break }
            guard n > 0 else { fail("incomplete frame") }
            offset += n
        }
    }
    return offset == count ? data : nil
}
func emit(_ data: Data) {
    data.withUnsafeBytes { raw in
        var offset = 0
        while offset < raw.count {
            let n = write(STDOUT_FILENO, raw.baseAddress!.advanced(by: offset), raw.count-offset)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { exit(errno == EPIPE ? 0 : 5) }
            offset += n
        }
    }
}
func be32(_ n: UInt32) -> Data { var v = n.bigEndian; return Data(bytes: &v, count: 4) }
func be64(_ n: UInt64) -> Data { var v = n.bigEndian; return Data(bytes: &v, count: 8) }
func packet(_ data: Data, _ flags: UInt64) { emit(be64(flags) + be32(UInt32(data.count)) + data) }
func word(_ data: Data, _ offset: Int) -> UInt32 {
    data.withUnsafeBytes { UInt32(bigEndian: $0.loadUnaligned(fromByteOffset: offset, as: UInt32.self)) }
}
let env = ProcessInfo.processInfo.environment
guard let fps = Int(env["AUTONOM_HOST_FPS"] ?? "60"), (1...120).contains(fps),
      let bitrate = Int(env["AUTONOM_HOST_BIT_RATE"] ?? "12000000"), (100000...100000000).contains(bitrate)
else { fail("invalid encoder options") }

final class Encoder {
    let fps: Int, bitrate: Int
    init(fps: Int, bitrate: Int) { self.fps = fps; self.bitrate = bitrate }
    var session: VTCompressionSession?
    var width = 0, height = 0
    var sentConfig = false
    var pending = 0
    var peakPending = 0
    var encoded = 0
    var dropped = 0
    var forceKey = true
    var lastPixel: CVPixelBuffer?
    var lastPTS: Int64 = 0
    let lock = NSLock()
    let outputLock = NSLock()
    let epoch = ProcessInfo.processInfo.systemUptime
    func complete() {
        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
        }
        session = nil; lastPixel = nil
    }
    func resize(_ w: Int, _ h: Int) {
        complete()
        width = w; height = h; sentConfig = false; forceKey = true
        let spec = [kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
                    kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true] as CFDictionary
        let attrs = [kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
                     kCVPixelBufferWidthKey: w, kCVPixelBufferHeightKey: h,
                     kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
        let status = VTCompressionSessionCreate(allocator: kCFAllocatorDefault,
            width: Int32(w), height: Int32(h), codecType: kCMVideoCodecType_H264,
            encoderSpecification: spec, imageBufferAttributes: attrs,
            compressedDataAllocator: nil, outputCallback: { ref, _, status, _, sample in
                guard let ref = ref else { return }
                let owner = Unmanaged<Encoder>.fromOpaque(ref).takeUnretainedValue()
                defer { owner.lock.lock(); owner.pending -= 1; owner.lock.unlock() }
                guard status == noErr, let sample = sample, CMSampleBufferDataIsReady(sample) else {
                    fail("hardware encode failed", 4)
                }
                owner.output(sample)
            }, refcon: Unmanaged.passUnretained(self).toOpaque(), compressionSessionOut: &session)
        guard status == noErr, let s = session else { fail("hardware encoder unavailable", 4) }
        let properties: [(CFString, CFTypeRef)] = [
            (kVTCompressionPropertyKey_RealTime, kCFBooleanTrue),
            (kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse),
            (kVTCompressionPropertyKey_ExpectedFrameRate, fps as CFTypeRef),
            (kVTCompressionPropertyKey_AverageBitRate, bitrate as CFTypeRef),
            (kVTCompressionPropertyKey_MaxKeyFrameInterval, fps as CFTypeRef),
            (kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel)]
        for (key, value) in properties {
            guard VTSessionSetProperty(s, key: key, value: value) == noErr else { fail("encoder property failed", 4) }
        }
        _ = VTSessionSetProperty(s, key: kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, value: kCFBooleanTrue)
        guard VTCompressionSessionPrepareToEncodeFrames(s) == noErr else { fail("encoder prepare failed", 4) }
        emit(be32(0x80000000) + be32(UInt32(w)) + be32(UInt32(h)))
    }
    func output(_ sample: CMSampleBuffer) {
        outputLock.lock(); defer { outputLock.unlock() }
        guard let format = CMSampleBufferGetFormatDescription(sample),
              let block = CMSampleBufferGetDataBuffer(sample) else { fail("missing encoded data", 4) }
        if !sentConfig {
            var config = Data()
            for i in 0..<2 {
                var pointer: UnsafePointer<UInt8>?, length = 0, sets = 0
                var nalLength: Int32 = 0
                guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,
                    parameterSetIndex: i, parameterSetPointerOut: &pointer, parameterSetSizeOut: &length,
                    parameterSetCountOut: &sets, nalUnitHeaderLengthOut: &nalLength) == noErr,
                    let p = pointer, nalLength == 4 else { fail("invalid H264 configuration", 4) }
                config.append(contentsOf: [0,0,0,1]); config.append(p, count: length)
            }
            packet(config, 1 << 62); sentConfig = true
        }
        let length = CMBlockBufferGetDataLength(block)
        var bytes = Data(count: length)
        let result = bytes.withUnsafeMutableBytes {
            CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!)
        }
        guard result == noErr else { fail("encoded buffer read failed", 4) }
        var output = Data(), offset = 0, key = false
        while offset+4 <= length {
            let size = Int(word(bytes, offset)); offset += 4
            guard size > 0 && size <= length-offset else { fail("invalid NAL size", 4) }
            if bytes[offset] & 31 == 5 { key = true }
            output.append(contentsOf: [0,0,0,1]); output.append(bytes[offset..<offset+size])
            offset += size
        }
        guard offset == length else { fail("incomplete NAL", 4) }
        let pts = UInt64(CMSampleBufferGetPresentationTimeStamp(sample).value)
        packet(output, pts | (key ? 1 << 61 : 0))
        lock.lock(); encoded += 1; lock.unlock()
    }
    func submit(_ pixel: CVPixelBuffer, force: Bool = false) {
        lock.lock()
        guard pending < 2 else { dropped += 1; lock.unlock(); return }
        pending += 1; peakPending = max(peakPending, pending); lock.unlock()
        // Monotonic local timestamps, including an explicitly requested idle IDR.
        lastPTS = max(lastPTS+1, Int64((ProcessInfo.processInfo.systemUptime-epoch)*1000000))
        let status = VTCompressionSessionEncodeFrame(session!, imageBuffer: pixel,
            presentationTimeStamp: CMTime(value: lastPTS, timescale: 1000000), duration: .invalid,
            frameProperties: force || forceKey ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary : nil,
            sourceFrameRefcon: nil, infoFlagsOut: nil)
        guard status == noErr else { fail("encode submission failed", 4) }
        forceKey = false
    }
    func consume(_ data: Data, _ w: Int, _ h: Int, _ stride: Int, _ format: Int, _ targetW: Int, _ targetH: Int) {
        let turn = format >> 8
        if turn > 0 {
            let rw = turn % 2 == 1 ? h : w, rh = turn % 2 == 1 ? w : h
            var rotated = Data(count: rw*rh*4)
            let result = data.withUnsafeBytes { src in rotated.withUnsafeMutableBytes { dst in
                var source = vImage_Buffer(data: UnsafeMutableRawPointer(mutating: src.baseAddress!), height: UInt(h), width: UInt(w), rowBytes: stride)
                var target = vImage_Buffer(data: dst.baseAddress!, height: UInt(rh), width: UInt(rw), rowBytes: rw*4)
                var background: [UInt8] = [0,0,0,255]
                return vImageRotate90_ARGB8888(&source, &target, UInt8(turn), &background, vImage_Flags(kvImageNoFlags))
            }}
            guard result == kvImageNoError else { fail("pixel rotation failed", 4) }
            consume(rotated, rw, rh, rw*4, format & 255, targetW, targetH)
            return
        }

        if targetW != width || targetH != height { resize(targetW,targetH) }
        lock.lock(); let busy = pending >= 2; lock.unlock()
        if busy { dropped += 1; return }
        guard let pool = VTCompressionSessionGetPixelBufferPool(session!) else { fail("no pixel pool", 4) }
        var optional: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &optional) == kCVReturnSuccess,
              let pixel = optional else { fail("pixel allocation failed", 4) }
        guard CVPixelBufferLockBaseAddress(pixel, []) == kCVReturnSuccess else { fail("pixel lock failed", 4) }
        let result: vImage_Error = data.withUnsafeBytes { raw in
            let scale = min(Double(w)/Double(targetW), Double(h)/Double(targetH))
            let cw = min(w, max(1, Int((Double(targetW)*scale).rounded())))
            let ch = min(h, max(1, Int((Double(targetH)*scale).rounded())))
            let x = (w-cw)/2, y = (h-ch)/2
            var source = vImage_Buffer(data: UnsafeMutableRawPointer(mutating: raw.baseAddress!.advanced(by: y*stride+x*4)),
                height: vImagePixelCount(ch), width: vImagePixelCount(cw), rowBytes: stride)
            var destination = vImage_Buffer(data: CVPixelBufferGetBaseAddress(pixel)!,
                height: vImagePixelCount(targetH), width: vImagePixelCount(targetW), rowBytes: CVPixelBufferGetBytesPerRow(pixel))
            var permutation: [UInt8] = format != 5 ? [2,1,0,3] : [0,1,2,3]
            let scaled = vImageScale_ARGB8888(&source, &destination, nil, vImage_Flags(kvImageNoFlags))
            guard scaled == kvImageNoError else { return scaled }
            if format == 17 {
                let flipped = vImageVerticalReflect_ARGB8888(&destination, &destination, vImage_Flags(kvImageNoFlags))
                guard flipped == kvImageNoError else { return flipped }
            }
            return vImagePermuteChannels_ARGB8888(&destination, &destination, &permutation, vImage_Flags(kvImageNoFlags))
        }
        CVPixelBufferUnlockBaseAddress(pixel, [])
        guard result == kvImageNoError else { fail("pixel conversion failed", 4) }
        lastPixel = pixel; submit(pixel)
    }
}
var name = Data("Autonom VideoToolbox".utf8)
name.append(Data(repeating: 0, count: 64-name.count))
emit(name + be32(0x68323634))
let encoder = Encoder(fps: fps, bitrate: bitrate)
while let header = readExact(32, allowEOF: true) {
    guard word(header,0) == 0x41463031 else { fail("invalid frame magic") }
    let length = Int(word(header,4)), w = Int(word(header,8)), h = Int(word(header,12))
    let stride = Int(word(header,16)), format = Int(word(header,20))
    if length == 0 {
        guard w == 0 && h == 0 && stride == 0 && format == 0 else { fail("invalid keyframe request") }
        encoder.forceKey = true
        if let pixel = encoder.lastPixel { encoder.submit(pixel, force: true) }
        continue
    }
    guard (2...8192).contains(w), (2...8192).contains(h),
          stride >= w*4, stride <= 8192*4, length == stride*h, length <= 64*1024*1024,
          format >> 8 <= 3, [1,5,17].contains(format & 255) else { fail("invalid frame geometry") }
    let targetW = Int(word(header,24)), targetH = Int(word(header,28))
    guard (2...8192).contains(targetW), (2...8192).contains(targetH), targetW % 2 == 0, targetH % 2 == 0,
          targetW*targetH*4 <= 64*1024*1024 else { fail("invalid target geometry") }
    encoder.consume(readExact(length)!, w,h,stride,format,targetW,targetH)
}
encoder.complete()
fputs("{\"encoded\":\(encoder.encoded),\"dropped\":\(encoder.dropped),\"peakPending\":\(encoder.peakPending),\"hardwareRequired\":true}\n", stderr)
