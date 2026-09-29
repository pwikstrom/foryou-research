"""The var-schema vocabulary: analysis roles, scales, and legacy role names.

Every contract module (activity, derived, scrape, annotation) validates its
fields against these, and ``recode_variables`` builds the var_schema with
them. Kept in core with no imports, so any module can use them at load time
without pulling in the config boot.
"""

# Analysis roles, keyed to the Correlations tab's unit of analysis (the
# collection-day group):
#   grouping   — defines the unit (collection_id, local_date)
#   comparison — group-constant categorical compared across in the
#                Group-differences sweep (ANOVA/PERMANOVA) and Colour-by
#   measure    — per-item property aggregated to a group-level quantity
#                (numeric -> day mean; categorical -> PCA components/entropy)
#   descriptor — group-constant context carried alongside the PCA frame for
#                hover/reference; excluded from the comparison sweep and the
#                Colour-by dropdown
#   skip       — hidden from analysis and recoding
VAR_SCHEMA_ROLES = ("grouping", "comparison", "measure", "descriptor", "skip")

# Legacy role vocabulary. Contract TOMLs are rewritten, but legacy
# registry field_metadata snapshots keep the old strings on disk forever;
# load_var_schema() normalizes every role through this map so downstream
# matchers only ever see the new values.
LEGACY_ROLE_ALIASES = {
    "group_factor": "grouping",
    "factor": "comparison",
    "feature": "measure",
}
VAR_SCHEMA_SCALES = (
    "categorical",
    "datetime",
    "list",
    "numeric",
    "raw",
    "text",
)


def normalize_role(value):
    """Map a legacy role string to its current name (identity for new values)."""
    return LEGACY_ROLE_ALIASES.get(value, value)
