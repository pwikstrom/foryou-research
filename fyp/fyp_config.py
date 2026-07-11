"""Back-compat alias for fyp.core.fyp_config — both paths are the same module object.

The alias preserves everything: ``fyp_cf`` (served by the real module's PEP 562
``__getattr__``), ``get_config()`` / ``initialize()`` / ``load_var_schema()``,
the private ``_apply_contract_*`` helpers the golden harness imports, and the
``PROJECT_ROOT`` / ``*_SCRIPT`` / ``PYTHON_EXEC`` constants re-exported from
``fyp.core.paths``.
"""
import sys

from fyp.core import fyp_config as _real

sys.modules[__name__] = _real
