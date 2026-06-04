from rag_system.config import Settings
from rag_system.models import RetrievalHit
from rag_system.observability import get_logger, metrics, retry_on_transient

logger = get_logger(__name__)


class BedrockCohereReranker:
    """Reranks retrieval hits using Cohere Rerank 3.5 via AWS Bedrock Agent Runtime."""

    def __init__(self, settings: Settings):
        self._client = settings.boto3_session().client("bedrock-agent-runtime")
        model_id = settings.bedrock_rerank_model_id
        region = settings.aws_region
        # Accept a full ARN as-is; otherwise construct one for foundation models
        self._model_arn = (
            model_id
            if model_id.startswith("arn:")
            else f"arn:aws:bedrock:{region}::foundation-model/{model_id}"
        )
        self._top_k = settings.rerank_top_k
        logger.info("BedrockCohereReranker initialised (model=%s)", self._model_arn)

    @retry_on_transient()
    def rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        if not hits:
            return []

        top_k = min(self._top_k, len(hits))
        logger.info(
            "Reranking %d hits (top_k=%d) via Bedrock",
            len(hits),
            top_k,
            extra={"hit_count": len(hits), "top_k": top_k, "model_id": self._model_arn},
        )
        metrics.observe("rag_rerank_input_hits", len(hits), {"model_id": self._model_arn})
        metrics.observe("rag_rerank_top_k", top_k, {"model_id": self._model_arn})
        response = self._client.rerank(
            queries=[
                {
                    "type": "TEXT",
                    "textQuery": {"text": query},
                }
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._model_arn},
                    "numberOfResults": top_k,
                },
            },
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": hit.chunk.text},
                    },
                }
                for hit in hits
            ],
        )

        reranked: list[RetrievalHit] = []
        for result in response["results"]:
            hit = hits[result["index"]]
            reranked.append(
                hit.model_copy(update={"score": float(result["relevanceScore"])})
            )
        metrics.observe("rag_rerank_output_hits", len(reranked), {"model_id": self._model_arn})
        logger.info(
            "Reranking complete: kept %d hits",
            len(reranked),
            extra={"hit_count": len(reranked), "model_id": self._model_arn},
        )
        return reranked
