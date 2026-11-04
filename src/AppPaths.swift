import Foundation

/// Bundle resources are immutable; recordings and staged worker modules are user-owned.
enum AppPaths {
    static let root: URL = {
        if let value = ProcessInfo.processInfo.environment["MEETING_OVERLAY_HOME"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MeetingOverlay", isDirectory: true)
    }()
    static var resources: URL { Bundle.main.resourceURL ?? Bundle.main.bundleURL }
    static var isPortableBundle: Bool {
        FileManager.default.fileExists(atPath: resources.appendingPathComponent("bundle-manifest.json").path)
    }
    static var modelProfile: String { ProcessInfo.processInfo.physicalMemory < 16 * 1024 * 1024 * 1024 ? "light" : "standard" }
    static var python: URL {
        isPortableBundle ? resources.appendingPathComponent("runtime/python/bin/python3.12")
            : root.appendingPathComponent("runtime/.venv/bin/python")
    }
    static var workerScript: URL {
        guard isPortableBundle,
              let data = try? Data(contentsOf: resources.appendingPathComponent("bundle-manifest.json")),
              let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let identifier = manifest["worker_id"] as? String else {
            return root.appendingPathComponent("src/worker.py")
        }
        return root.appendingPathComponent("portable/\(identifier)-\(modelProfile)/src/worker.py")
    }
    static var workerEnvironment: [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["HF_HUB_OFFLINE"] = "1"
        environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        environment["TOKENIZERS_PARALLELISM"] = "false"
        environment["MEETING_SUMMARY_MODEL_LABEL"] = modelProfile == "light" ? "Qwen3.5 4B" : "Qwen3.5 9B"
        environment["OMP_NUM_THREADS"] = "4"
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        return environment
    }
}
