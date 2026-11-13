from decimal import Decimal

from pydantic import BaseModel


class Order(BaseModel):
    symbol: str
    qty: Decimal
    total: Decimal | None = None
