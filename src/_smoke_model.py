import warnings
from rag_system.sql_lab.router import SqlAnalyzeRequest

with warnings.catch_warnings():
    warnings.simplefilter("error")
    try:
        m = SqlAnalyzeRequest(columns=["a"], rows=[], rowCount=3)
        print("camelCase rowCount ->", m.row_count)
    except Exception as e:
        print("ERR camelCase:", type(e).__name__, e)

# snake_case (populate_by_name)
m2 = SqlAnalyzeRequest(columns=["a"], rows=[], row_count=7)
print("snake_case row_count ->", m2.row_count)

import pydantic
print("pydantic", pydantic.VERSION)
print("fields:", {k: (v.alias, v.is_required()) for k, v in SqlAnalyzeRequest.model_fields.items()})
