# Whether a tool call may run. Tool facts (permission, agents, max window) arrive in
# input.tool_spec from the Gateway's registry, so there is no data file to drift from it.
package opsteam.tool_access

default allow := false

allow if {
	well_formed
	count(reasons) == 0
}

decision := {"allow": allow, "reasons": reasons, "obligations": obligations}

well_formed if {
	is_string(input.principal.tenant_id)
	is_string(input.agent)
	is_string(input.tool)
	is_string(input.tool_spec.permission)
	is_array(input.tool_spec.allowed_agents)
	is_number(input.tool_spec.max_window_seconds)
	is_string(input.integration.tenant_id)
	is_string(input.integration.status)
	is_object(input.args)
}

reasons contains "invalid_input" if not well_formed

reasons contains "invalid_input" if {
	is_object(input.incident)
	# object.get, not a bare ref: `not is_string(<missing>)` is skipped under `with input as`.
	not is_string(object.get(input.incident, "tenant_id", null))
}

reasons contains "cross_tenant" if input.principal.tenant_id != input.integration.tenant_id

# Ad-hoc calls (dev CLI) have no incident; when one is given it must be the caller's.
reasons contains "cross_tenant" if {
	is_object(input.incident)
	input.principal.tenant_id != input.incident.tenant_id
}

reasons contains "integration_disabled" if input.integration.status != "active"

reasons contains "tool_not_allowed_for_agent" if not input.agent in input.tool_spec.allowed_agents

reasons contains "tool_not_read" if input.tool_spec.permission != "READ"

reasons contains "window_too_large" if {
	input.tool_spec.max_window_seconds > 0
	input.args.window_seconds > input.tool_spec.max_window_seconds
}

default obligations := {}

obligations := {"max_window_seconds": input.tool_spec.max_window_seconds} if {
	allow
	input.tool_spec.max_window_seconds > 0
}
