from dataclasses import dataclass

@dataclass
class VisitConfig:
    target_url: str
    min_pre_wait: float = 3.0
    max_pre_wait: float = 5.0
    min_stay: int = 60
    max_stay: int = 180
    referrer: str = "about:blank"
    max_workers: int = 3
    visits_per_worker: int = 1

    def clamp(self) -> "VisitConfig":
        if self.min_pre_wait > self.max_pre_wait:
            self.min_pre_wait, self.max_pre_wait = self.max_pre_wait, self.min_pre_wait
        if self.min_stay > self.max_stay:
            self.min_stay, self.max_stay = self.max_stay, self.min_stay
        if self.max_workers < 1:
            self.max_workers = 1
        if self.visits_per_worker < 1:
            self.visits_per_worker = 1
        return self
