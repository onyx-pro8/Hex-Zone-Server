"""Shared pytest configuration.

SQLite in-memory tests do not load SpatiaLite; geoalchemy2's default ``after_create`` calls
``RecoverGeometryColumn``, which requires SpatiaLite. Patch SQLite DDL hooks so ``Base.metadata.create_all``
works for API tests that never exercise geometry columns.
"""

from __future__ import annotations

import geoalchemy2.admin.dialects.sqlite as ga_sqlite

_real_sqlite_after_create = ga_sqlite.after_create
_real_sqlite_before_drop = ga_sqlite.before_drop
_real_sqlite_after_drop = ga_sqlite.after_drop


def _sqlite_after_create_tests(table, bind, **kw):
    dialect = bind.dialect
    if dialect.name != "sqlite":
        return _real_sqlite_after_create(table, bind, **kw)

    from geoalchemy2.admin.dialects.common import _check_spatial_type
    from geoalchemy2.types import Geometry, Geography

    table.columns = table.info.pop("_saved_columns")
    for col in table.columns:
        if _check_spatial_type(col.type, Geometry, dialect) and hasattr(col, "_actual_type"):
            col.type = col._actual_type
            del col._actual_type

    for col in table.columns:
        if _check_spatial_type(col.type, (Geometry, Geography), dialect) and getattr(col.type, "spatial_index", False):
            pass

    for idx in table.info.pop("_after_create_indexes", []):
        table.indexes.add(idx)


def _sqlite_before_drop_tests(table, bind, **kw):
    dialect = bind.dialect
    if dialect.name != "sqlite":
        return _real_sqlite_before_drop(table, bind, **kw)
    from geoalchemy2.admin.dialects.common import setup_create_drop

    setup_create_drop(table, bind)


def _sqlite_after_drop_tests(table, bind, **kw):
    dialect = bind.dialect
    if dialect.name != "sqlite":
        return _real_sqlite_after_drop(table, bind, **kw)
    if "_saved_columns" in table.info:
        table.columns = table.info.pop("_saved_columns")


ga_sqlite.after_create = _sqlite_after_create_tests
ga_sqlite.before_drop = _sqlite_before_drop_tests
ga_sqlite.after_drop = _sqlite_after_drop_tests
