# 0005. Sectionless annotation prompt with inferred scale

Date: 2026-07-27

## Context

The annotation contract grouped its fields into prompt sections, and every
field declared its recode `scale` explicitly. Both made the contract harder to
edit than it needed to be: the sections carried no information the model
used, and the scale is determined by the field's shape in all but one case.

## Decision

- The prompt is flat: a header, one bullet per field, and a footer. Contracts
  no longer declare sections.
- `scale` is inferred from the field's shape (array → `list`, int →
  `numeric`, enum → `categorical`). Only a free-text string field must
  declare it (`categorical` for short labels vs `text` for long prose picks
  the recode function); validation enforces this, and an explicit `scale`
  still overrides the inference.

## Consequences

- Legacy contracts with `[[section]]` tables (stored candidates, registered
  versions) keep rendering byte-identically, so their version hashes do not
  change.
- The authoring keys are documented in
  [contracts.md](../contracts.md#annotation-fields).
