from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
needle = "        super().__init__(**kwargs)"
replacement = (
    '        kwargs.setdefault("operator_scopes", '
    '["operator.read", "operator.write", "operator.admin"])\n'
    "        super().__init__(**kwargs)"
)
if source.count(needle) != 1:
    raise SystemExit(f"unexpected ResilientGateway constructor count: {source.count(needle)}")
path.write_text(source.replace(needle, replacement), encoding="utf-8")
print("operator.admin scope injected")
