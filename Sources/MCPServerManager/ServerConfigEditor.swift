import SwiftUI

struct ServerConfigEditor: View {
    @ObservedObject var model: ManagerModel
    var serverID: String
    @State private var text = ""
    @State private var original = ""
    @State private var revision = ""
    @State private var loading = true
    @State private var saving = false
    @State private var error: String?
    @State private var saved = false
    private var dirty: Bool { text != original }
    private var validJSON: Bool {
        guard let data = text.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data) else { return false }
        return object is [String: Any]
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionTitle(title: "MCP configuration")
                TransportTag(text: "JSON")
            }
            if loading {
                HStack { ProgressView().controlSize(.small); Text("Loading configuration…").font(.system(size: 11)).foregroundStyle(Palette.muted) }.frame(maxWidth: .infinity, minHeight: 130)
            } else {
                TextEditor(text: $text)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.88))
                    .scrollContentBackground(.hidden)
                    .padding(10)
                    .frame(height: min(410, max(185, CGFloat(text.components(separatedBy: "\n").count) * 15 + 24)))
                    .background(Palette.panel, in: RoundedRectangle(cornerRadius: 9))
                    .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(error == nil ? Palette.stroke : Palette.red.opacity(0.45)))
                    .disabled(saving || revision.isEmpty)
                    .accessibilityLabel("MCP server configuration JSON")
                HStack(spacing: 8) {
                    Button("Save configuration") { Task { await save() } }
                        .buttonStyle(SoftButtonStyle(primary: true)).disabled(!dirty || !validJSON || saving || model.busy || revision.isEmpty)
                    Button("Reset") { Task { await load(useDraft: false) } }
                        .buttonStyle(SoftButtonStyle()).disabled(saving || model.busy)
                    Button("Format") { format() }
                        .buttonStyle(SoftButtonStyle()).disabled(!validJSON || saving || revision.isEmpty)
                    Spacer(minLength: 0)
                    if saving { ProgressView().controlSize(.small) }
                    else if saved && !dirty { Image(systemName: "checkmark.circle.fill").foregroundStyle(Palette.green).help("Configuration saved") }
                    else if dirty { Circle().fill(Palette.amber).frame(width: 6, height: 6).help("Unsaved changes") }
                }
                if let error { Text(error).font(.system(size: 10)).foregroundStyle(Palette.red).textSelection(.enabled) }
                else if dirty && !validJSON { Text("Enter a valid JSON object to save.").font(.system(size: 10)).foregroundStyle(Palette.amber) }
                Text("Includes editable credentials. Changes are saved directly to this server’s entry in the common mcp.json file.")
                    .font(.system(size: 10)).foregroundStyle(Palette.muted).lineSpacing(3)
            }
        }
        .task(id: serverID) { await load(useDraft: true) }
        .onChange(of: text) { _, _ in
            saved = false
            if !revision.isEmpty { model.configDrafts[serverID] = ServerConfigDraft(text: text, original: original, revision: revision) }
        }
    }
    private func load(useDraft: Bool) async {
        if useDraft, let draft = model.configDrafts[serverID], draft.text != draft.original {
            text = draft.text; original = draft.original; revision = draft.revision; loading = false
            return
        }
        loading = true; error = nil; saved = false
        do {
            let data = try await Backend.run("control", request: ["action": "server_config", "server_id": serverID])
            let document = try JSONDecoder().decode(ServerConfigDocument.self, from: data)
            original = document.config_json; revision = document.revision; text = document.config_json
            model.configDrafts.removeValue(forKey: serverID)
        } catch { self.error = error.localizedDescription }
        loading = false
    }
    private func save() async {
        guard !saving, !model.busy else { return }
        saving = true; model.busy = true; model.operation = "Saving server configuration…"; error = nil
        do {
            let data = try await Backend.run("control", request: ["action": "save_server_config", "server_id": serverID, "config_json": text, "revision": revision])
            model.accept(try JSONDecoder().decode(Snapshot.self, from: data))
            model.configDrafts.removeValue(forKey: serverID)
            await load(useDraft: false)
            saved = error == nil
        } catch { self.error = error.localizedDescription }
        saving = false; model.busy = false
    }
    private func format() {
        guard let data = text.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data),
              let formatted = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let string = String(data: formatted, encoding: .utf8) else { return }
        text = string
    }
}
