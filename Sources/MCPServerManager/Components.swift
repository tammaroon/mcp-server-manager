import SwiftUI

struct BrandMark: View {
    var size: CGFloat = 36
    var body: some View {
        Image(systemName: "point.3.connected.trianglepath.dotted")
            .font(.system(size: size * 0.52, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(LinearGradient(colors: [Palette.blue, Color(red: 0.31, green: 0.40, blue: 0.80)], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: size * 0.28))
            .overlay(RoundedRectangle(cornerRadius: size * 0.28).strokeBorder(.white.opacity(0.16)))
    }
}
struct StatusDot: View {
    var color: Color
    var body: some View { Circle().fill(color).frame(width: 6, height: 6) }
}
struct StatusPill: View {
    var text: String
    var color: Color = Palette.muted
    var body: some View {
        HStack(spacing: 6) { StatusDot(color: color); Text(text).font(.system(size: 10, weight: .medium)) }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(color.opacity(0.08), in: Capsule())
            .overlay(Capsule().strokeBorder(color.opacity(0.2)))
            .foregroundStyle(color)
    }
}
struct TransportTag: View {
    var text: String
    var fontSize: CGFloat = 9
    var body: some View {
        Text(text).font(.system(size: fontSize, weight: .medium, design: .monospaced))
            .foregroundStyle(Palette.muted).padding(.horizontal, 5).padding(.vertical, 2)
            .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 4))
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Palette.stroke))
    }
}
struct ServerIcon: View {
    var server: Server
    var size: CGFloat = 34
    var body: some View {
        Image(systemName: server.glyph).font(.system(size: size * 0.39, weight: .medium))
            .foregroundStyle(server.enabled ? Palette.blue : Palette.muted)
            .frame(width: size, height: size).background(Palette.raised, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Palette.stroke))
    }
}
struct SoftButtonStyle: ButtonStyle {
    var primary = false
    var fontSize: CGFloat = 12
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: fontSize, weight: .medium))
            .padding(.horizontal, 12).padding(.vertical, 8)
            .foregroundStyle(primary ? Color.white : Color.white.opacity(0.82))
            .background(primary ? Color(red: 0.37, green: 0.46, blue: 0.88) : Palette.raised, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(primary ? Palette.blue.opacity(0.35) : Palette.stroke))
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}
struct SectionTitle: View {
    var title: String
    var trailing: String = ""
    var fontSize: CGFloat = 9
    var body: some View {
        HStack { Text(title.uppercased()).tracking(1.3); Spacer(); Text(trailing) }
            .font(.system(size: fontSize, weight: .semibold)).foregroundStyle(Palette.muted)
    }
}
struct InfoLine: View {
    var label: String
    var value: String
    var tint: Color = .white.opacity(0.75)
    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            Text(label).foregroundStyle(Palette.muted).frame(width: 100, alignment: .leading)
            Text(value).foregroundStyle(tint).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
        }.font(.system(size: 11)).padding(.vertical, 5)
    }
}
struct EmptyState: View {
    var symbol: String
    var title: String
    var message: String
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 30, weight: .light)).foregroundStyle(Palette.blue)
            Text(title).font(.system(size: 17, weight: .semibold))
            Text(message).font(.system(size: 12)).foregroundStyle(Palette.muted).multilineTextAlignment(.center).frame(maxWidth: 280)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(30)
    }
}
struct Banner: View {
    var text: String
    var isError: Bool
    var dismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: isError ? "exclamationmark.triangle" : "checkmark.circle").foregroundStyle(isError ? Palette.amber : Palette.green)
            Text(text).font(.system(size: 11)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 10)) }.buttonStyle(.plain)
        }.padding(12).background((isError ? Palette.amber : Palette.green).opacity(0.08))
    }
}
