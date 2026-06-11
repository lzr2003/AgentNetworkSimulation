import random
import json

class SkillRegistry:
    _skills = {}

    @classmethod
    def register(cls, name, func):
        cls._skills[name] = func

    @classmethod
    def execute(cls, name, **kwargs):
        if name not in cls._skills:
            return {"status": "error", "result": f"Skill {name} not found", "data": {}}
        try:
            return cls._skills[name](**kwargs)
        except Exception as e:
            return {"status": "error", "result": str(e), "data": {}}

# ==========================================
# 9x9 棋盘且小兵不死机制的全局内存状态机
# ==========================================
class MinesweeperEngine9x9:
    SIZE = 9
    TOTAL_MINES = 10
    
    def __init__(self):
        self.initialized = False
        self.board = [[0 for _ in range(self.SIZE)] for _ in range(self.SIZE)]  # 0:安全, 1:雷
        self.revealed = [[False for _ in range(self.SIZE)] for _ in range(self.SIZE)]
        # 记录地雷被发现并报告的历史，格式：{(x, y): "reported_by_soldier_id"}
        self.discovered_mines = {} 
        # 跟踪每个 Agent 在当前 Round 的行动，防止单 Round 内无效刷屏
        self.round_action_registry = {}  # { round_id: { soldier_id: (x, y) } }

    def init_map(self):
        if self.initialized:
            return
        mines_placed = 0
        while mines_placed < self.TOTAL_MINES:
            x = random.randint(0, self.SIZE - 1)
            y = random.randint(0, self.SIZE - 1)
            if self.board[y][x] == 0:
                self.board[y][x] = 1
                mines_placed += 1
        self.initialized = True

    def count_adjacent_mines(self, cx, cy):
        count = 0
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < self.SIZE and 0 <= ny < self.SIZE:
                    if self.board[ny][nx] == 1:
                        count += 1
        return count

    def check_victory(self):
        # 胜利条件：所有非雷网格（9x9 - 10 = 71个）都已经被安全翻开
        revealed_safe_count = 0
        for y in range(self.SIZE):
            for x in range(self.SIZE):
                if self.revealed[y][x] and self.board[y][x] == 0:
                    revealed_safe_count += 1
        total_safe_required = (self.SIZE * self.SIZE) - self.TOTAL_MINES
        return revealed_safe_count == total_safe_required, revealed_safe_count

_engine = MinesweeperEngine9x9()

# ==========================================
# 核心技能函数
# ==========================================

def initialize_minesweeper_map(**kwargs):
    """
    初始化 9x9 扫雷棋盘技能
    """
    _engine.init_map()
    safe_spots = []
    for y in range(_engine.SIZE):
        for x in range(_engine.SIZE):
            if _engine.board[y][x] == 0:
                safe_spots.append((x, y))
    
    start_x, start_y = random.choice(safe_spots)
    _engine.revealed[start_y][start_x] = True
    adj_mines = _engine.count_adjacent_mines(start_x, start_y)

    return {
        "status": "success",
        "result": "9x9 Minesweeper map initialized successfully at Round 0.",
        "data": {
            "map_size": f"{_engine.SIZE}x{_engine.SIZE}",
            "total_mines": _engine.TOTAL_MINES,
            "initial_safe_zone": {"x": start_x, "y": start_y, "adjacent_mines": adj_mines}
        }
    }

def move_and_reconnaissance(**kwargs):
    """
    前线小兵移动与侦察技能（小兵不死，踩雷仅作情报报告）
    参数: soldier_id (str), x (int), y (int), current_round (int)
    """
    soldier_id = kwargs.get("soldier_id")
    x = kwargs.get("x")
    y = kwargs.get("y")
    current_round = kwargs.get("current_round", 0)

    if x is None or y is None or not (0 <= x < _engine.SIZE) or not (0 <= y < _engine.SIZE):
        return {"status": "error", "result": "Coordinates out of bounds (0-8 allowed).", "data": {}}

    # 【防爆熔断逻辑】: 拦截同一 Round 重复行动
    if current_round not in _engine.round_action_registry:
        _engine.round_action_registry[current_round] = {}
    
    if soldier_id in _engine.round_action_registry[current_round]:
         return {
            "status": "error", 
            "result": f"Rejection: Soldier {soldier_id} already moved in Round {current_round}.", 
            "data": {}
         }
    
    # 登记当前轮次行动并揭开格子
    _engine.round_action_registry[current_round][soldier_id] = (x, y)
    _engine.revealed[y][x] = True

    # 1. 踩雷判定 —— 修改：小兵不死，只进行记录和情报反馈
    if _engine.board[y][x] == 1:
        _engine.discovered_mines[(x, y)] = soldier_id
        is_victory, safe_count = _engine.check_victory()
        return {
            "status": "success",
            "result": f"REPORT: Soldier {soldier_id} detected a MINE at ({x}, {y}) in Round {current_round}. Soldier remains active.",
            "data": {
                "current_round": current_round,
                "scouted_cell": {"x": x, "y": y},
                "event": "FOUND_MINE_REPORT",
                "is_alive": True, 
                "game_over_trigger": is_victory,
                "map_progress": f"{safe_count}/71 safe cells revealed."
            }
        }

    # 2. 安全区判定
    adjacent_mines = _engine.count_adjacent_mines(x, y)
    is_victory, safe_count = _engine.check_victory()

    return {
        "status": "success",
        "result": f"Soldier {soldier_id} scouted ({x}, {y}) safely. Adjacent mines: {adjacent_mines}.",
        "data": {
            "current_round": current_round,
            "scouted_cell": {"x": x, "y": y},
            "event": "SAFE_RECON",
            "is_alive": True,
            "adjacent_mines": adjacent_mines,
            "game_over_trigger": is_victory,
            "map_progress": f"{safe_count}/71 safe cells revealed."
        }
    }

def query_game_status(**kwargs):
    """
    查询当前 9x9 地图全局推进状态 — 返回完整棋盘网格供指挥官决策
    网格符号: ? = 未探明, 数字 = 安全(相邻雷数), 💣 = 已发现地雷
    """
    current_round = kwargs.get("current_round", -1)
    is_victory, safe_count = _engine.check_victory()
    total_safe_cells = (_engine.SIZE * _engine.SIZE) - _engine.TOTAL_MINES

    status_str = "RUNNING"
    if is_victory:
        status_str = "ALL_MAP_CLEARED_VICTORY"

    visual_grid = []
    for y in range(_engine.SIZE):
        row = []
        for x in range(_engine.SIZE):
            if (x, y) in _engine.discovered_mines:
                row.append("💣")
            elif _engine.revealed[y][x]:
                adj = _engine.count_adjacent_mines(x, y)
                row.append(str(adj))
            else:
                row.append("?")
        visual_grid.append(row)

    revealed_cells = []
    for y in range(_engine.SIZE):
        for x in range(_engine.SIZE):
            if _engine.revealed[y][x] and (x, y) not in _engine.discovered_mines:
                revealed_cells.append({
                    "x": x, "y": y,
                    "adjacent_mines": _engine.count_adjacent_mines(x, y),
                })

    risk_summary = {}
    for c in revealed_cells:
        key = c["adjacent_mines"]
        risk_summary.setdefault(key, 0)
        risk_summary[key] += 1

    grid_display = "\n".join(" ".join(row) for row in visual_grid)

    return {
        "status": "success",
        "result": f"棋盘状态 (Round {current_round}):\n{grid_display}\n\n图例: ?=未探明, 数字=安全格相邻雷数, 💣=已发现地雷\n已揭示 {safe_count}/{total_safe_cells} 安全格",
        "data": {
            "current_simulation_round": current_round,
            "game_state": status_str,
            "cells_revealed": safe_count,
            "total_safe_cells_required": total_safe_cells,
            "completion_percentage": f"{(safe_count / total_safe_cells) * 100:.2f}%",
            "total_discovered_mines_count": len(_engine.discovered_mines),
            "discovered_mines_coordinates": [f"({k[0]},{k[1]})" for k in _engine.discovered_mines.keys()],
            "board_grid": visual_grid,
            "revealed_cells": revealed_cells,
            "risk_summary": risk_summary,
            "unrevealed_count": (_engine.SIZE * _engine.SIZE) - len(revealed_cells) - len(_engine.discovered_mines),
        }
    }

# 注册技能
SkillRegistry.register("initialize_minesweeper_map", initialize_minesweeper_map)
SkillRegistry.register("move_and_reconnaissance", move_and_reconnaissance)
SkillRegistry.register("query_game_status", query_game_status)