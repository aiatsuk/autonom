import XCTest
import UIKit

// Targetless UI-test runner: never installs, launches, or resets the app under test.
// A per-run nonce and per-request ID bind the simulator mailbox to its owner.
@MainActor
final class BridgeTests: XCTestCase {
    private struct TargetElement {
        let root: XCUIElement
        let type: XCUIElement.ElementType
        let identifier: String
        let label: String
        let frame: CGRect
    }
    private var snapshots: [String: TargetElement] = [:]
    private let mailbox = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent("Documents/autonom-ui", isDirectory: true)

    func testServe() throws {
        continueAfterFailure = false
        let token = ProcessInfo.processInfo.environment["AUTONOM_RUNNER_TOKEN"] ?? ""
        guard !token.isEmpty else { throw BridgeError("missing_owner", "No owner token") }
        try FileManager.default.createDirectory(at: mailbox, withIntermediateDirectories: true)
        try write(["token": token, "protocol": 1], to: "ready.json")
        defer { try? FileManager.default.removeItem(at: mailbox.appendingPathComponent("ready.json")) }
        var handled = Set<String>()
        var idleDeadline = Date().addingTimeInterval(300)
        while Date() < idleDeadline {
            let requestURL = mailbox.appendingPathComponent("request.json")
            if let data = try? Data(contentsOf: requestURL),
               let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               request["token"] as? String == token,
               let id = request["id"] as? String, !handled.contains(id) {
                handled.insert(id) // Mark before dispatch: an uncertain mutation is never replayed.
                idleDeadline = Date().addingTimeInterval(300)
                try? FileManager.default.removeItem(at: requestURL)
                var response: [String: Any] = ["id": id, "token": token]
                do {
                    response["result"] = try execute(request)
                    response["ok"] = true
                } catch let error as BridgeError {
                    response["ok"] = false
                    response["error_code"] = error.code
                    response["error"] = error.message
                } catch {
                    response["ok"] = false
                    response["error_code"] = "xcuitest_failed"
                    response["error"] = String(describing: error)
                }
                try write(response, to: "response.json")
                if request["command"] as? String == "stop" { return }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
    }

    private func execute(_ request: [String: Any]) throws -> [String: Any] {
        let command = request["command"] as? String ?? ""
        if command == "stop" { return [:] }
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let bundle = request["app_id"] as? String ?? ""
        guard !bundle.isEmpty else { throw BridgeError("app_id_required", "Select a session with an app ID") }
        let app = XCUIApplication(bundleIdentifier: bundle)
        switch command {
        case "snapshot":
            snapshots.removeAll()
            var nodes: [[String: Any]] = []
            if app.state == .runningForeground || app.state == .runningBackground {
                try append(app, path: "app", depth: 0, to: &nodes)
            }
            // Permission sheets and keyboards can belong to SpringBoard.
            for (i, element) in springboard.alerts.allElementsBoundByIndex.enumerated() {
                try append(element, path: "system-alert-\(i)", depth: 0, to: &nodes)
            }
            if !springboard.keyboards.allElementsBoundByIndex.isEmpty {
                for (i, element) in springboard.keyboards.allElementsBoundByIndex.enumerated() {
                    try append(element, path: "system-keyboard-\(i)", depth: 0, to: &nodes)
                }
            }
            if bundle == "com.apple.springboard" && nodes.isEmpty {
                try append(springboard, path: "app", depth: 0, to: &nodes)
            }
            return ["nodes": nodes, "screen": geometry(springboard)]
        case "geometry":
            return ["screen": geometry(springboard)]
        case "tap":
            if let ref = request["ref"] as? String {
                guard let saved = snapshots[ref],
                      saved.identifier == request["identifier"] as? String,
                      saved.label == request["label"] as? String else {
                    throw BridgeError("stale_ui_element", "Refresh the UI tree before tapping")
                }
                let predicate = NSPredicate(format: "identifier == %@ AND label == %@", saved.identifier, saved.label)
                let candidates = saved.root.descendants(matching: saved.type).matching(predicate).allElementsBoundByIndex
                let matches = candidates.count == 1 ? candidates : candidates.filter {
                    abs($0.frame.midX - saved.frame.midX) < 2 && abs($0.frame.midY - saved.frame.midY) < 2
                }
                guard matches.count == 1, let element = matches.first,
                      element.exists, element.isEnabled, element.isHittable else {
                    throw BridgeError("stale_ui_element", "The observed element is no longer uniquely hittable; refresh the tree")
                }
                element.tap()
            } else {
                let coordinate = try point(request, "x", "y", springboard)
                if let duration = request["duration"] as? Double { coordinate.press(forDuration: duration) }
                else { coordinate.tap() }
            }
            snapshots.removeAll()
            return ["dispatched": true]
        case "swipe":
            let start = try point(request, "x1", "y1", springboard)
            let end = try point(request, "x2", "y2", springboard)
            start.press(forDuration: request["duration"] as? Double ?? 0.1, thenDragTo: end)
            snapshots.removeAll()
            return ["dispatched": true]
        case "type":
            guard app.keyboards.count > 0 || springboard.keyboards.count > 0 else {
                throw BridgeError("no_focused_field", "Tap a text field before typing")
            }
            app.typeText(request["text"] as? String ?? "")
            snapshots.removeAll()
            return ["dispatched": true]
        case "home":
            XCUIDevice.shared.press(.home)
            snapshots.removeAll()
            return ["dispatched": true]
        default: throw BridgeError("unsupported_capability", "Unknown bridge command")
        }
    }

    private func geometry(_ app: XCUIApplication) -> [String: Any] {
        let rect = app.frame
        return ["width": rect.width, "height": rect.height, "units": "points",
                "source": "xcuitest.springboard", "orientation": XCUIDevice.shared.orientation.rawValue]
    }

    private func point(_ request: [String: Any], _ xKey: String, _ yKey: String,
                       _ app: XCUIApplication) throws -> XCUICoordinate {
        guard let x = request[xKey] as? Double, let y = request[yKey] as? Double,
              x.isFinite, y.isFinite, app.frame.width > 0, app.frame.height > 0,
              x >= 0, y >= 0, x < app.frame.width, y < app.frame.height else {
            throw BridgeError("coordinate_space_mismatch", "Point is outside the measured screen")
        }
        return app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: y))
    }

    private func append(_ element: XCUIElement, path: String, depth: Int,
                        to nodes: inout [[String: Any]]) throws {
        guard nodes.count < 1500, depth < 40 else { return }
        let snapshot = try element.snapshot()
        appendSnapshot(snapshot, element: element, path: path, depth: depth, to: &nodes)
    }

    private func appendSnapshot(_ snapshot: XCUIElementSnapshot, element: XCUIElement,
                                path: String, depth: Int, to nodes: inout [[String: Any]]) {
        guard nodes.count < 1500, depth < 40 else { return }
        let rect = snapshot.frame
        let ref = UUID().uuidString
        snapshots[ref] = TargetElement(root: element, type: snapshot.elementType,
                                       identifier: snapshot.identifier, label: snapshot.label, frame: rect)
        nodes.append(["type": typeName(snapshot.elementType),
                      "element_type_number": snapshot.elementType.rawValue,
                      "AXLabel": snapshot.label, "AXValue": snapshot.value as? String ?? "",
                      "identifier": snapshot.identifier, "enabled": snapshot.isEnabled,
                      "selected": snapshot.isSelected, "_depth": depth,
                      "frame": ["x": rect.minX, "y": rect.minY, "width": rect.width, "height": rect.height],
                      "xcuitest_ref": ref])
        for (index, child) in snapshot.children.enumerated() {
            appendSnapshot(child, element: element,
                           path: "\(path)/\(index)", depth: depth + 1, to: &nodes)
        }
    }

    private func typeName(_ type: XCUIElement.ElementType) -> String {
        switch type {
        case .application: return "Application"
        case .window: return "Window"
        case .button: return "Button"
        case .staticText: return "StaticText"
        case .textField: return "TextField"
        case .secureTextField: return "SecureTextField"
        case .textView: return "TextView"
        case .searchField: return "SearchField"
        case .scrollView: return "ScrollView"
        case .table: return "Table"
        case .collectionView: return "CollectionView"
        case .cell: return "Cell"
        case .switch: return "Switch"
        case .slider: return "Slider"
        case .link: return "Link"
        case .navigationBar: return "NavigationBar"
        case .tabBar: return "TabBar"
        case .toolbar: return "Toolbar"
        case .alert: return "Alert"
        case .sheet: return "Sheet"
        case .keyboard: return "Keyboard"
        case .key: return "Button"
        case .image: return "Image"
        default: return "Other"
        }
    }

    private func write(_ value: [String: Any], to name: String) throws {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        try data.write(to: mailbox.appendingPathComponent(name), options: .atomic)
    }
}

private struct BridgeError: Error {
    let code: String
    let message: String
    init(_ code: String, _ message: String) { self.code = code; self.message = message }
}
