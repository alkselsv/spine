# Register link association boundary (#613)

An observed register link is an immutable claim about one accepted Google Sheets
row revision. It records the extraction profile, exact row and provenance IDs,
the cell locator, and a digest of the observed identifier evidence. It does not
assert a canonical business relation.

The handoff models `request`, `meeting`, `client`, `project`,
`historical_estimate`, `source_estimate`, `final_estimate`, and `drive_material`
roles. Until their business aggregates exist, the handoff remains unresolved and
names the future owner: `commercial_request`, `commercial_meeting`,
`commercial_client`, `commercial_project`, `commercial_estimate_history`,
`commercial_source_estimate`, `commercial_final_estimate`, or
`commercial_drive_material` respectively.

Current acceptance binds the exact row and target `SourceRevision` in one
workspace and environment. The target must be an accepted canonical revision,
and its stored Google provider identity must match the explicit Drive file ID
claimed by the row. This ticket's resolvable target form is a Google Drive
`SourceRevision`; a source from another provider or a future Spine business
entity remains unresolved until that identity contract is modeled. Display
names, textual similarity, URLs, and cross-tenant identifiers cannot establish
this identity. The claim and acceptance are append-only and replay by ID must
return the same handoff. Acceptance is bound to the actor and environment in the
verified persistence context.

An accepted association records provenance only. It does not authorize document
disclosure, grant an `AccessPolicy`, or establish `PricingBasis` authority; the
pricing-policy boundary remains with #586.
