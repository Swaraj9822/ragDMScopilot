import warnings
from rag_system.sql_lab.router import SqlAnalyzeRequest

with warnings.catch_warnings():
    warnings.simplefilter("error")
    try:
        SqlAnalyzeRequest.model_json_schema(by_alias=True)
        print("schema built clean (no warning from this model)")
    except Warning as w:
        print("WARNING from this model:", w)
