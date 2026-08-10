# Loom recipe implementation status

The current implementation is split across the Loom checkout and this ETL
checkout:

- Loom now has strict versioned recipe documents and canonical digests in
  `internal/dataframe/recipe`.
- Typed, backend-neutral expressions live in `internal/dataframe/expression`.
- Generic traversal, expansion, collision, and dynamic-column evaluation is in
  `internal/dataframe/recipeeval`.
- Immutable registration and all-output-before-publish execution is in
  `internal/dataframe/recipeexec`.
- The production semantic boundary is now `semantic.BuildRecipePlan` followed
  by `semantic.ResolveRecipePlan`; lexical aliases, FHIR types/cardinality,
  expansion item scope, identities, fragments, and dynamic-map plans are
  checked before execution.
- Typed recipe expressions now lower through physical `CALL`/`LITERAL` IR,
  and `lower.LowerResolvedRecipePlan` preserves all outputs, traversals,
  expansions, identities, and dynamic maps without resource/output-name
  dispatch.
- `recipecontrol` provides transport-neutral validate/explain/resolve/preview
  operations; durable recipe storage has an Arango adapter; materialization
  exposes an atomic multi-output publication transaction contract.
- The default five-output translation is data in
  `internal/dataframe/recipe/default_aced.json`.
- Legacy parity comparison is in `conformance/legacydataframer`; its checked-in
  fixture is a harness smoke fixture, while the real `META` corpus still needs
  to be generated from the pinned `gen3_util` commit.
- The selector/traversal subset can lower through Loom's existing scoped AQL
  compiler using `recipecompile.CompileBundle`.

Loom now has the storage-backed recipe engine, GraphQL control-plane fields,
durable recipe registry, and atomic ClickHouse bundle adapter. The remaining
implementation boundary is narrower: UUID3/UUID5 must execute exactly in the
physical path, resolved dynamic columns must be emitted by row execution, and
materialization must insert bounded batches instead of retaining complete
outputs. The real `META` parity and recovery suite also remains a required
release gate. The existing baby-step ETL path therefore continues to load a
complete generation into Loom, export generation-scoped NDJSON, and run the
legacy DataFramer for parity-safe publication.

The detailed production completion sequence is in
[LOOM_RECIPE_PRODUCTION_COMPLETION_PLAN.md](LOOM_RECIPE_PRODUCTION_COMPLETION_PLAN.md).
The actionable plan for the five remaining integration gaps is in
[LOOM_RECIPE_FINAL_INTEGRATION_PLAN.md](LOOM_RECIPE_FINAL_INTEGRATION_PLAN.md).

The remaining closure work is defined in
[LOOM_RECIPE_REMAINING_GAPS_PLAN.md](LOOM_RECIPE_REMAINING_GAPS_PLAN.md).
