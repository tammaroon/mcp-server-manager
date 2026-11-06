import AppKit
import SwiftUI

struct ClientsView: View {
    @ObservedObject var model: ManagerModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 13) {
                    Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 25)).foregroundStyle(Palette.blue)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("A single connection. Shared upstreams.").font(.system(size: 14, weight: .medium))
                        Text("Each app launches a lightweight stdio bridge to your local gateway. Credentials stay on this Mac.").font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(4)
                    }
                }.padding(20).frame(maxWidth: .infinity, alignment: .leading).background(Palette.blue.opacity(0.06), in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Palette.blue.opacity(0.12)))
                SectionTitle(title: "Manager connections", trailing: "4 supported clients")
                ForEach(model.snapshot?.clients ?? []) { client in
                    VStack(alignment: .leading, spacing: 16) {
                        HStack(spacing: 13) {
                            Image(systemName: client.glyph).font(.system(size: 20)).foregroundStyle(Palette.blue).frame(width: 42, height: 42).background(Palette.raised, in: RoundedRectangle(cornerRadius: 11))
                            VStack(alignment: .leading, spacing: 6) {
                                Text(client.name).font(.system(size: 14, weight: .medium))
                                Text(client.error != nil ? "Configuration needs attention" : client.bridge_count > 0 ? "Bridge running · \(client.bridge_count) connection(s)" : client.configured ? "Bridge configured · awaiting connection" : client.installed ? "Discovered · not connected to manager" : "No existing config detected").font(.system(size: 11)).foregroundStyle(Palette.muted)
                            }
                            Spacer()
                            StatusPill(text: client.bridge_count > 0 ? "Running" : client.configured ? "Configured" : "Not configured", color: client.bridge_count > 0 ? Palette.green : Palette.muted)
                        }
                        Text(client.config_path).font(.system(size: 10, design: .monospaced)).foregroundStyle(Palette.muted).textSelection(.enabled)
                        if let error = client.error { Text(error).font(.system(size: 10)).foregroundStyle(Palette.amber) }
                        HStack(spacing: 8) {
                            Button(client.configured ? "Review bridge update" : "Review connection") { model.preview(client) }.buttonStyle(SoftButtonStyle(primary: !client.configured)).disabled(model.busy)
                            Button("Open config") { model.openConfig(client.config_path) }.buttonStyle(SoftButtonStyle())
                            Spacer()
                            if client.configured { Button("Review removal") { model.preview(client, remove: true) }.font(.system(size: 10)).foregroundStyle(Palette.muted).buttonStyle(.plain).disabled(model.busy) }
                        }
                    }.padding(20).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Palette.stroke))
                }
                Label("Existing direct server entries are preserved for rollback. Disable migrated direct entries in the owning app to avoid duplicate tools.", systemImage: "info.circle").font(.system(size: 10)).foregroundStyle(Palette.muted).lineSpacing(4)
            }.padding(28)
        }
    }
}
struct ConfigReviewView: View {
    @ObservedObject var model: ManagerModel
    var change: ConfigReview
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack { Image(systemName: "doc.text.magnifyingglass").foregroundStyle(Palette.blue); Text(change.remove ? "Review bridge removal" : "Review manager connection").font(.system(size: 20, weight: .semibold)); Spacer() }
            Text(change.path).font(.system(size: 10, design: .monospaced)).foregroundStyle(Palette.muted).textSelection(.enabled)
            Text("A private backup is created before applying. The change is rejected if the file has been edited since this review.").font(.system(size: 12)).foregroundStyle(Palette.muted).lineSpacing(3)
            ScrollView([.horizontal, .vertical]) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(change.diff.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                        Text(line.isEmpty ? " " : line).font(.system(size: 11, design: .monospaced)).foregroundStyle(line.hasPrefix("+") ? Palette.green : line.hasPrefix("-") ? Palette.red : Palette.muted).textSelection(.enabled).padding(.vertical, 2).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(16)
            }.frame(height: 340).background(Palette.panel, in: RoundedRectangle(cornerRadius: 9)).overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Palette.stroke))
            Text(change.warning).font(.system(size: 11)).foregroundStyle(Palette.amber).lineSpacing(3)
            if let error = model.error { Text(error).font(.system(size: 11)).foregroundStyle(Palette.red) }
            HStack { Button("Cancel") { dismiss() }.buttonStyle(SoftButtonStyle()); Spacer(); if model.busy { ProgressView().controlSize(.small) }; Button("Apply change") { model.apply(change) }.buttonStyle(SoftButtonStyle(primary: true)).disabled(model.busy) }
        }.padding(28).frame(width: 680).background(Palette.background).preferredColorScheme(.dark)
    }
}
struct AddServerView: View {
    @ObservedObject var model: ManagerModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var transport = "HTTP"
    @State private var endpoint = ""
    @State private var args = ""
    @State private var cwd = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Add upstream server").font(.system(size: 20, weight: .semibold))
            Text("Add the server once, then choose which agent clients can access it.").font(.system(size: 12)).foregroundStyle(Palette.muted)
            field("Name", placeholder: "My MCP server", value: $name)
            Picker("Transport", selection: $transport) { Text("Remote HTTP").tag("HTTP"); Text("Local stdio").tag("stdio") }.pickerStyle(.segmented)
            field(transport == "HTTP" ? "MCP endpoint" : "Executable", placeholder: transport == "HTTP" ? "https://example.com/mcp" : "/absolute/path/to/executable", value: $endpoint)
            if transport == "stdio" {
                VStack(alignment: .leading, spacing: 8) { Text("Arguments · one per line").font(.system(size: 11)); TextEditor(text: $args).font(.system(size: 11, design: .monospaced)).frame(height: 70).padding(5).background(Palette.panel).overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Palette.stroke)) }
                field("Working directory (optional)", placeholder: "/path/to/project", value: $cwd)
            }
            Label("HTTP credentials are saved directly in each server’s common mcp.json entry, readable only by your user account. Keep secrets out of URLs and arguments.", systemImage: "lock.shield").font(.system(size: 10)).foregroundStyle(Palette.muted).lineSpacing(3)
            HStack { Button("Cancel") { dismiss() }.buttonStyle(SoftButtonStyle()); Spacer(); Button("Add server") {
                var definition: [String: Any] = transport == "HTTP" ? ["url": endpoint] : ["command": endpoint, "args": args.components(separatedBy: .newlines).filter { !$0.isEmpty }]
                if !cwd.isEmpty && transport == "stdio" { definition["cwd"] = cwd }
                model.perform("add", values: ["name": name, "definition": definition], label: "Adding upstream…"); dismiss()
            }.buttonStyle(SoftButtonStyle(primary: true)).disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || endpoint.isEmpty || model.busy) }
        }.padding(28).frame(width: 480).background(Palette.background).preferredColorScheme(.dark)
    }
    private func field(_ label: String, placeholder: String, value: Binding<String>) -> some View { VStack(alignment: .leading, spacing: 8) { Text(label).font(.system(size: 11)); TextField(placeholder, text: value).textFieldStyle(.roundedBorder) } }
}
