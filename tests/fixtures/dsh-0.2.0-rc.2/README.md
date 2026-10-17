This synthetic checkpoint was produced by the real 0.2.0-rc.2 kernel against
the local Messages API simulator in tests/test_dsh.py. It contains no QQ data
or credentials. Only its temporary absolute workspace path was replaced with
`__LEGACY_WORKSPACE__`; the compatibility test relocates it to a new temporary
directory and refreshes the external retention deadline. Keep the old event
and replay formats so future upgrades exercise genuine checkpoint compatibility.
