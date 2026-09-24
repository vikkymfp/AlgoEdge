from dataclasses import dataclass


@dataclass
class PaperBroker:
    cash: float
    position: int = 0

    def execute(self, action: str, price: float) -> None:
        if action == "buy" and self.position == 0 and self.cash >= price:
            self.cash -= price
            self.position = 1
        elif action == "sell" and self.position == 1:
            self.cash += price
            self.position = 0

    def equity(self, price: float) -> float:
        return self.cash + (self.position * price)
