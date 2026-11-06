import Foundation

@main enum ManagerStateCheck {
    @MainActor static func main() throws {
        let model = ManagerModel()
        let data = Data(#"{"servers":[],"clients":[],"errors":[],"settings":{"check_interval":0},"daemon":{"status":"reachable","started":0,"last_checked":null,"socket":"/tmp/fixture","bridge_count":0},"version":"0.1"}"#.utf8)
        model.accept(try JSONDecoder().decode(Snapshot.self, from: data))
        precondition(model.gatewayAvailable, "Snapshot must establish daemon availability")
        model.connectionFailed("Disconnected")
        guard !model.gatewayAvailable, model.aggregate.0 == "Gateway unavailable", model.error == "Disconnected", model.snapshot != nil else {
            FileHandle.standardError.write(Data("FAIL: daemon loss retained a healthy state or discarded the catalog\n".utf8))
            exit(1)
        }
        let fixture = Data(#"{"id":"fixture","name":"jira","transport":"HTTP","endpoint":"https://example.test/mcp","enabled":true,"supported":true,"clients":[],"tool_allowlists":{},"auth":"Not validated","credential_set":false,"credential_header":"Authorization","status":"failed","tools":[],"diagnostic":"Fixture"}"#.utf8)
        let base = try JSONSerialization.jsonObject(with: fixture) as! [String: Any]
        let cases: [(String, String, String, Bool?, Bool)] = [
            ("jira", "auth_required", "HTTP", true, true),
            ("jira", "failed", "HTTP", true, true),
            ("jira", "reachable", "HTTP", true, false),
            ("jira", "not_checked", "HTTP", true, false),
            ("jira", "disabled", "HTTP", true, false),
            ("jira", "failed", "HTTP", false, false),
            ("local", "failed", "stdio", true, false),
            ("webexapis", "auth_required", "HTTP", nil, true)
        ]
        for (name, status, transport, signIn, expected) in cases {
            var object = base
            object["name"] = name; object["status"] = status; object["transport"] = transport
            object["enabled"] = status != "disabled"
            if let signIn { object["can_sign_in"] = signIn }
            let server = try JSONDecoder().decode(Server.self, from: JSONSerialization.data(withJSONObject: object))
            guard server.shouldOfferDropdownLogin == expected else { fatalError("Wrong dropdown login eligibility for \(name), \(status), \(transport)") }
        }
        print("PASS: dropdown sign-in appears only for failed/auth-required servers with browser login")
        print("PASS: daemon loss invalidates availability and retains the catalog for diagnosis")
    }
}
