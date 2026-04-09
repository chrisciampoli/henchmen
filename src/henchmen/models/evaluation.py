"""Evaluation models — quality metrics from Vertex AI GenAI Evaluation."""

from pydantic import BaseModel, Field


class EvaluationResult(BaseModel):
    """Quality scores produced by the GenAI Evaluation Service after an operative run."""

    fulfillment_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Task fulfillment (0-1)")
    tool_call_valid_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Tool call validity (0-1)")
    safety_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Safety compliance (0-1)")
    overall_quality: float = Field(default=0.0, ge=0.0, le=1.0, description="Weighted overall quality (0-1)")
    evaluation_error: str | None = Field(default=None, description="Error message if evaluation failed")
