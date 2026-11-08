// Lanceur natif de BAO.app : un vrai exécutable signé, pour que macOS rattache à BAO
// (et non à /bin/zsh) les autorisations Micro et Documents. Il lance `bin/jarvis run` et attend.
import Foundation

let info = Bundle.main.infoDictionary ?? [:]
let root = info["BAORoot"] as? String ?? "\(NSHomeDirectory())/Documents/JARVIS"
let logURL = URL(fileURLWithPath: "\(NSHomeDirectory())/Library/Logs/BAO.log")
if !FileManager.default.fileExists(atPath: logURL.path) {
    FileManager.default.createFile(atPath: logURL.path, contents: nil)
}
let log = try? FileHandle(forWritingTo: logURL)
log?.seekToEndOfFile()
log?.write("[\(Date())] lancement de BAO depuis \(root)\n".data(using: .utf8)!)

let task = Process()
task.executableURL = URL(fileURLWithPath: "/bin/zsh")
task.arguments = ["\(root)/bin/jarvis", "run"]
task.currentDirectoryURL = URL(fileURLWithPath: root)
if let log = log {
    task.standardOutput = log
    task.standardError = log
}
// arrêter l'app arrête BAO proprement
signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { task.terminate(); exit(0) }
term.resume()
do {
    try task.run()
} catch {
    log?.write("impossible de lancer BAO : \(error)\n".data(using: .utf8)!)
    exit(1)
}
DispatchQueue.global().async {
    task.waitUntilExit()
    exit(task.terminationStatus)
}
dispatchMain()
