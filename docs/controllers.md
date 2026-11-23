# Field supply

One host owns the field ledger, producer lock and publication checkpoint. Run
the supplier there. Search nodes claim work through the ordinary worker CLI.

The operator imports `tools/field_controller.py` from the checkout. Call
`serve(cycle, lock_path, report)` with a zero-argument cycle and a callback
that atomically writes the status. Use `standalone_cycle(site)` when the
operator also owns local worker recovery. Use `distributed_cycle(site)` on
the fixed supply host when the search machines supervise their own workers.
Stop the old supplier before transferring the ledger and ownership.

The site adapter checks handover markers, maintains storage, reads coordinator
counts and returns the number of accepted unpublished results and the
publication hold flag. It chooses and prepares one field, or expands the plan
when there is no field to prepare. In standalone operation it also recovers
the local worker and records recovery errors. The reviewer and publisher run
as independent services in both deployments.
