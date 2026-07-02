"""EventBus compatibility layer after removing simulated packet data."""

from typing import Any, Dict


class PacketRecord:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("PacketRecord simulation removed; use pcap data.")


class PacketRecorder:
    @classmethod
    def record(cls, **kw):
        raise RuntimeError("Simulated packet recording removed; use tcpdump pcap.")

    @classmethod
    def record_inbound(cls, *args, **kwargs):
        raise RuntimeError("Simulated packet recording removed; use tcpdump pcap.")

    @classmethod
    def record_outbound(cls, *args, **kwargs):
        raise RuntimeError("Simulated packet recording removed; use tcpdump pcap.")

    @classmethod
    def reset(cls):
        return None

    @classmethod
    def get_records(cls, *args, **kwargs):
        from agent_network.real_packet_store import query_packets
        return query_packets(*args, **kwargs)

    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        from agent_network.real_packet_store import packet_stats
        return packet_stats()

    @classmethod
    def get_wireshark_view(cls, *args, **kwargs):
        from agent_network.real_packet_store import wireshark_lines
        return wireshark_lines(*args, **kwargs)


class EventBus:
    def __init__(self):
        self.subscribers = []

    def subscribe(self, callback):
        self.subscribers.append(callback)

    def emit(self, event):
        for callback in list(self.subscribers):
            try:
                callback(event)
            except Exception:
                pass
