"""AgentHub stub"""
from enum import Enum

class RoutingStrategy(Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_LOADED = "least_loaded"
    RANDOM = "random"
    AFFINITY = "affinity"

    @classmethod
    def from_string(cls, s: str):
        for member in cls:
            if member.value == s:
                return member
        return cls.ROUND_ROBIN

class ScalingPolicy:
    def __init__(self, min_agents=1, max_agents=10, scale_up_threshold=0.8, scale_down_threshold=0.2, cooldown_seconds=30):
        self.min_agents = min_agents
        self.max_agents = max_agents
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.cooldown_seconds = cooldown_seconds

class AgentHub:
    def __init__(self): pass
    def start(self): pass
    def set_routing_strategy(self, strategy): pass
def get_hub(): return AgentHub()
