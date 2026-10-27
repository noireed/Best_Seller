from typing import Literal
from pydantic import BaseModel, Field

class Decision(BaseModel):
    symbol: str = Field(pattern=r"^[0-9A-Za-z]{6,12}$")
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0, le=1)
    quantity: int = Field(ge=0)
    reason: str
