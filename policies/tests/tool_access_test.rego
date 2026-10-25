package opsteam.tool_access_test

import data.opsteam.tool_access

base := {
	"principal": {"tenant_id": "tnt_a", "type": "agent", "scopes": []},
	"agent": "metrics",
	"tool": "metrics.query_range",
	"tool_spec": {"permission": "READ", "allowed_agents": ["metrics"], "max_window_seconds": 21600},
	"integration": {"tenant_id": "tnt_a", "type": "prometheus", "status": "active"},
	"resource": {"service": "checkout", "environment": "shop"},
	"args": {"window_seconds": 3600},
}

denied_for(inp, reason) if {
	d := tool_access.decision with input as inp
	not d.allow
	reason in d.reasons
}

test_read_call_allowed_with_window_obligation if {
	d := tool_access.decision with input as base
	d.allow
	d.reasons == set()
	d.obligations == {"max_window_seconds": 21600}
}

test_no_window_limit_means_no_obligation if {
	inp := json.patch(base, [{"op": "replace", "path": "/tool_spec/max_window_seconds", "value": 0}])
	d := tool_access.decision with input as inp
	d.allow
	d.obligations == {}
}

test_cross_tenant_integration_denied if {
	denied_for(json.patch(base, [{"op": "replace", "path": "/integration/tenant_id", "value": "tnt_b"}]), "cross_tenant")
}

test_cross_tenant_incident_denied if {
	denied_for(object.union(base, {"incident": {"tenant_id": "tnt_b"}}), "cross_tenant")
}

test_own_incident_allowed if {
	tool_access.allow with input as object.union(base, {"incident": {"tenant_id": "tnt_a"}})
}

test_agent_not_in_allowlist_denied if {
	denied_for(object.union(base, {"agent": "logs"}), "tool_not_allowed_for_agent")
}

test_write_tool_denied if {
	denied_for(json.patch(base, [{"op": "replace", "path": "/tool_spec/permission", "value": "WRITE"}]), "tool_not_read")
}

test_window_over_max_denied if {
	denied_for(object.union(base, {"args": {"window_seconds": 21601}}), "window_too_large")
}

test_disabled_integration_denied if {
	denied_for(json.patch(base, [{"op": "replace", "path": "/integration/status", "value": "disabled"}]), "integration_disabled")
}

test_missing_fields_denied if {
	denied_for(object.remove(base, ["tool_spec"]), "invalid_input")
	denied_for(json.patch(base, [{"op": "replace", "path": "/principal/tenant_id", "value": 7}]), "invalid_input")
}

test_incident_without_tenant_denied if {
	denied_for(object.union(base, {"incident": {"severity": "high"}}), "invalid_input")
}

test_empty_input_denied if {
	denied_for({}, "invalid_input")
}

test_denial_carries_no_obligations if {
	d := tool_access.decision with input as object.union(base, {"agent": "logs"})
	d.obligations == {}
}
