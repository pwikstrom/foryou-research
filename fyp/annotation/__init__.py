"""Gemini annotation, contracts/versioning, recoding, and presentation.

This package ``__init__`` is deliberately inert (no imports): several members
(annotation_contract/annotation_versioning/var_presentation) are imported
*mid-boot* by ``fyp_config.load_var_schema``, and machine_annotation /
recode_variables carry module ``__getattr__``s for lazy config constants.
"""
