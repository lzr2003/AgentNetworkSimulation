"""
百万级仿真框架 — 基于 scale_config.json 统计建模，不逐个孵化 agent

设计原则:
- 不创建 agent 实例列表（内存不可行）
- 用统计公式估算拓扑边数和流量
- 应用网络约束防止组合爆炸
- 输出可被前端/仿真引擎直接消费的聚合结果

用法:
    python scale_spawner.py -n 1000000 --seed 42
    python scale_spawner.py -n 500000 --scale 2.0  # scale_factor 倍增所有 spawn_count
"""

import json
import os
import random
import math


class ScaleSimulator:
    def __init__(self, config_path):
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, config_path), "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.categories = self.config["agent_categories"]
        self.network_rules = self.config["network_generation_rules"]
        self.traffic_rules = self.config["traffic_generation_rules"]
        self.global_constraints = self.config["network_global_constraints"]
        self.mapping = self.config["mapping_rules"]["rules"]
        self.scaling = self.config["scaling_parameters"]

    def compute_agent_distribution(self, target_total=None, scale_factor=1.0, seed=42):
        """
        按 base * scale_factor 计算各类别 agent 数量，归一化到 target_total。
        不创建实例，仅返回 {category_id: count}。
        """
        random.seed(seed)
        total_base = sum(c["spawn_count"]["base"] for c in self.categories)

        # 按 base 比例分配
        raw = {}
        for c in self.categories:
            base_count = int(c["spawn_count"]["base"] * scale_factor * c["spawn_count"]["scale_factor"])
            raw[c["category_id"]] = base_count

        if target_total:
            current_total = sum(raw.values())
            if current_total > 0:
                ratio = target_total / current_total
                raw = {k: max(1, int(v * ratio)) for k, v in raw.items()}
                # 修正差值
                diff = target_total - sum(raw.values())
                keys = sorted(raw.keys(), key=lambda k: raw[k], reverse=True)
                for i in range(abs(diff)):
                    idx = i % len(keys)
                    if diff > 0:
                        raw[keys[idx]] += 1
                    else:
                        if raw[keys[idx]] > 1:
                            raw[keys[idx]] -= 1

        return raw

    def estimate_topology(self, agent_counts):
        """
        估算各子网的边数。
        公式: edges = min(agent_pairs * density, max_total_edges, 全局约束)
        不枚举边，仅返回统计量。
        """
        category_counts = {c["category_id"]: c for c in self.categories}
        subnets = []

        for rule in self.network_rules:
            subnet_edges = 0
            edge_details = []

            if rule["generation_rule"] in ("INTRA_CATEGORY_FULL_INTER_CATEGORY_SPARSE", "BIPARTITE"):
                src_cats = rule["source_categories"]
                tgt_cats = rule["target_categories"]

                for sc in src_cats:
                    for tc in tgt_cats:
                        sc_count = agent_counts.get(sc, 0)
                        tc_count = agent_counts.get(tc, 0)
                        if sc_count == 0 or tc_count == 0:
                            continue

                        if rule["generation_rule"] == "INTRA_CATEGORY_FULL_INTER_CATEGORY_SPARSE":
                            density = rule["edge_density"]["intra"] if sc == tc else rule["edge_density"]["inter"]
                        else:
                            density = rule["edge_density"].get("inter", rule["edge_density"].get("intra", 0.001))

                        # 每个源 agent 的连接数 = min(tc_count * density, 类别peers上限)
                        sc_constraint = next((c for c in self.categories if c["category_id"] == sc), None)
                        max_peers = sc_constraint["topology_constraints"]["max_peers_per_agent"] if sc_constraint else 25
                        affinity = 1.0
                        if sc_constraint:
                            affinity = sc_constraint["topology_constraints"]["connection_affinity"].get(tc, 0.1)

                        pairs = sc_count * tc_count
                        if sc == tc:
                            pairs = sc_count * (sc_count - 1) // 2  # 无向图去重

                        category_edges = int(pairs * density * affinity)
                        # 应用 per-agent 上限
                        category_edges = min(category_edges, sc_count * max_peers)

                        if category_edges > 0:
                            edge_details.append({
                                "source_cat": sc, "target_cat": tc,
                                "estimated_edges": category_edges,
                                "density_used": round(density * affinity, 6),
                            })
                        subnet_edges += category_edges

            elif rule["generation_rule"] == "STAR_HUB":
                hub_density = rule["edge_density"]["hub_to_spoke"]
                hub_total = sum(agent_counts.get(hc, 0) for hc in rule["hub_categories"])

                for sc in rule["spoke_categories"]:
                    sc_count = agent_counts.get(sc, 0)
                    sc_constraint = next((c for c in self.categories if c["category_id"] == sc), None)
                    max_peers = sc_constraint["topology_constraints"]["max_peers_per_agent"] if sc_constraint else 25

                    category_edges = int(sc_count * hub_total * hub_density)
                    category_edges = min(category_edges, sc_count * min(max_peers, hub_total))

                    if category_edges > 0:
                        edge_details.append({
                            "source_cat": sc, "target_cat": "|".join(rule["hub_categories"]),
                            "estimated_edges": category_edges,
                            "density_used": round(hub_density, 6),
                        })
                    subnet_edges += category_edges

            # 应用子网上限
            max_edges = rule.get("max_total_edges", subnet_edges * 2)
            subnet_edges = min(subnet_edges, max_edges)

            subnets.append({
                "sub_id": rule["sub_id"],
                "topology_type": rule["topology_type"],
                "description": rule["description"],
                "total_edges": subnet_edges,
                "category_breakdown": edge_details,
            })

        # 全局约束
        global_max = self.global_constraints["max_total_edges_across_all_subnets"]
        total_edges = sum(s["total_edges"] for s in subnets)
        if total_edges > global_max:
            scale = global_max / total_edges
            for s in subnets:
                s["total_edges"] = int(s["total_edges"] * scale)

        return {
            "subnets": subnets,
            "total_edges_all_subnets": sum(s["total_edges"] for s in subnets),
            "global_max": global_max,
        }

    def estimate_traffic(self, agent_counts, rounds=10):
        """
        估算三类流量的总吞吐量。
        不模拟每个 agent，仅用统计公式。
        """
        traffic = {"EAST_WEST": {"total_kb": 0, "total_requests": 0},
                    "NORTH_SOUTH": {"total_kb": 0, "total_requests": 0},
                    "INTERNAL": {"total_kb": 0, "total_requests": 0}}

        for cat in self.categories:
            cat_id = cat["category_id"]
            count = agent_counts.get(cat_id, 0)
            if count == 0:
                continue

            bp = cat["behavior_profile"]
            mix = bp["traffic_mix"]
            payloads = bp["avg_payload_kb"]

            # 每类 agent 每轮平均动作数
            actions = bp["actions_per_10_rounds"]
            non_idle_actions = sum(
                (rng[0] + rng[1]) / 2  # 取中位数
                for k, rng in actions.items() if k != "idle"
            )
            avg_actions_per_round = non_idle_actions / 10

            for ttype in ["EAST_WEST", "NORTH_SOUTH", "INTERNAL"]:
                if mix.get(ttype, 0) == 0:
                    continue
                reqs = int(count * avg_actions_per_round * mix[ttype] * rounds)
                kbs = reqs * payloads.get(ttype, 0)
                traffic[ttype]["total_kb"] += kbs
                traffic[ttype]["total_requests"] += reqs

        total_kb = sum(t["total_kb"] for t in traffic.values())
        return {
            "rounds": rounds,
            "EAST_WEST_kb": int(traffic["EAST_WEST"]["total_kb"]),
            "NORTH_SOUTH_kb": int(traffic["NORTH_SOUTH"]["total_kb"]),
            "INTERNAL_kb": int(traffic["INTERNAL"]["total_kb"]),
            "EAST_WEST_requests": int(traffic["EAST_WEST"]["total_requests"]),
            "NORTH_SOUTH_requests": int(traffic["NORTH_SOUTH"]["total_requests"]),
            "INTERNAL_requests": int(traffic["INTERNAL"]["total_requests"]),
            "total_tb": round(total_kb / 1_073_741_824, 2),
            "total_requests": int(sum(t["total_requests"] for t in traffic.values())),
        }

    def summary(self, agent_counts, topology, traffic):
        return {
            "total_agents": sum(agent_counts.values()),
            "category_distribution": agent_counts,
            "llm_enabled_agents": int(sum(
                agent_counts[c["category_id"]] * c["model_backbone"]["llm_ratio"]
                for c in self.categories
            )),
            "total_topology_edges": topology["total_edges_all_subnets"],
            "total_traffic_tb_per_10_rounds": traffic["total_tb"],
            "subnets": [{ "sub_id": s["sub_id"], "edges": s["total_edges"] } for s in topology["subnets"]],
        }


# ============================================================
# 百万级采样生成（用于前端展示，按比例采样少量 agent）
# ============================================================
def sample_agents_for_display(agent_counts, sample_total=100, seed=42):
    """
    从统计分布中按比例采样少量 agent 实例，供前端拓扑可视化。
    每个采样 agent 代表其类别中的一个统计Bucket。
    """
    random.seed(seed)
    total = sum(agent_counts.values())
    if total == 0:
        return []
    samples = []
    for cat_id, count in agent_counts.items():
        cat_sample = max(1, int(sample_total * count / total))
        for i in range(min(cat_sample, count)):
            samples.append({
                "agent_id": f"{cat_id}_sample_{i+1:03d}",
                "category_id": cat_id,
                "represents": max(1, count // cat_sample),
            })
    # 裁剪到 sample_total
    if len(samples) > sample_total:
        samples = random.sample(samples, sample_total)
    return samples


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="百万级仿真统计建模器")
    parser.add_argument("--total", "-n", type=int, default=1000000, help="目标 agent 总数")
    parser.add_argument("--seed", "-s", type=int, default=42, help="随机种子")
    parser.add_argument("--rounds", "-r", type=int, default=10, help="仿真轮数")
    parser.add_argument("--scale", type=float, default=1.0, help="scale_factor 倍增所有 spawn_count")
    parser.add_argument("--sample", type=int, default=100, help="前端展示采样数")
    parser.add_argument("--out", "-o", type=str, default="", help="输出JSON路径")
    args = parser.parse_args()

    sim = ScaleSimulator("scale_config.json")
    agent_counts = sim.compute_agent_distribution(target_total=args.total, scale_factor=args.scale, seed=args.seed)
    topology = sim.estimate_topology(agent_counts)
    traffic = sim.estimate_traffic(agent_counts, rounds=args.rounds)
    summary = sim.summary(agent_counts, topology, traffic)
    display_samples = sample_agents_for_display(agent_counts, sample_total=args.sample, seed=args.seed)

    output = {
        "config": "scale_config.json",
        "seed": args.seed,
        "rounds": args.rounds,
        "summary": summary,
        "topology": topology,
        "traffic": traffic,
        "display_samples": {
            "count": len(display_samples),
            "note": f"从 {summary['total_agents']} agents 中采样 {len(display_samples)} 个供前端展示",
            "agents": display_samples,
        },
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"Output: {args.out}")

    # 打印摘要
    print(f"========== 百万级仿真建模 ==========")
    print(f"Target: {args.total:,} agents (scale_factor={args.scale})")
    print(f"Actual: {summary['total_agents']:,} agents")
    print(f"LLM-enabled: {summary['llm_enabled_agents']:,} ({summary['llm_enabled_agents']/summary['total_agents']*100:.1f}%)")
    print()
    for cat_id, cnt in sorted(agent_counts.items(), key=lambda x: -x[1]):
        cat = next(c for c in sim.categories if c["category_id"] == cat_id)
        personas = ", ".join(f"{p['role']}({p['ratio']*100:.0f}%)" for p in cat["persona_templates"][:2])
        print(f"  {cat_id:18s}: {cnt:>8,}  [{personas}]")
    print()
    print(f"--- 拓扑 ---")
    print(f"Total edges: {topology['total_edges_all_subnets']:,}")
    for s in topology["subnets"]:
        print(f"  {s['sub_id']:20s} {s['topology_type']:10s}: {s['total_edges']:>12,} edges")
    print()
    print(f"--- 流量估算 ({args.rounds} rounds) ---")
    for ttype in ["EAST_WEST", "NORTH_SOUTH", "INTERNAL"]:
        tb = traffic[f"{ttype}_kb"] / 1_073_741_824
        reqs = traffic[f"{ttype}_requests"]
        print(f"  {ttype:12s}: {tb:>8.2f} TB  ({reqs:>12,} requests)")
    print(f"  {'TOTAL':12s}: {traffic['total_tb']:>8.2f} TB  ({traffic['total_requests']:>12,} requests)")
    print()
    print(f"Display samples: {len(display_samples)} agents (for frontend rendering)")
